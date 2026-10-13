"""集中主題＋事件驅動策略的「代理」回測（LLM 無法回測，以候選池分數前 n 名代替 LLM 選股）。

每日流程與實盤相同（只差選股者）：
  D 日收盤後：候選池 screen → 代理選股 proxy_picks（前 11 名，前 5 名「高」、其餘「中」）→ 換股紀律 discipline（修正模式：
  持有 < 3 日不賣、每日最多換 4 檔、停損 −12%、高波動事件前一日不新增）→ build_portfolio（高 9%／中 6%、湊檔股、現金、
  Active Share）→ 無交易帶 apply_band → plan_orders（同實盤：委託後現金比 ≥ 2%，不足時等比例縮減買進）→ derive_orders。
  T 日以成交均價成交、扣費稅；以收盤價估值。時序、分割處理與 backtest.run_backtest 相同。
代理沒有「新的正面事件」可引用，所以停損一律出場。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .agent.plan import BUY_BUFFER, plan_orders
from .backtest import TRADING_DAYS, _reject_unaffordable_buys, perf
from .config import ACTIVE_SHARE_MIN, CASH_MAX, FEE_RATE, INITIAL_CAPITAL, MAX_HOLDINGS, MIN_HOLDINGS, TAX_RATE, max_weight
from .data.etf_holdings import check_target_weights
from .data.prices import Panel
from .ledger import Ledger
from .strategy.candidates import CandidateParams, proxy_picks, screen
from .strategy.conviction import (ConvictionParams, HeldPick, apply_band, blackout_events, build_portfolio,
                                  discipline, guard_triggered, pick_return)
from .strategy.factors import MIN_HISTORY, factors_on


@dataclass
class ProxyParams:
    n_picks: int = 11
    n_high: int = 5
    keep_rank: int | None = None          # 遲滯：已持有且候選分數名次 ≤ keep_rank 者優先保留（None = 嚴格取前 n 名）
    fixed: tuple = ()                     # 敏感度：固定列入的 (ticker, level)，例如 (("2330", "high"),)
    sticky_levels: bool = True            # 已持有者沿用原信心等級（代理不因名次在第 5／6 名間跳動就每天升降級）


@dataclass
class ConvictionBacktest:
    nav: pd.Series
    daily: pd.DataFrame
    violations: dict[str, int]
    unfilled: int = 0
    rejected_buys: int = 0
    build_errors: int = 0
    picks_log: list[dict] = field(default_factory=list)

    def summary(self, start: str | None = None, end: str | None = None) -> dict:
        n = self.nav
        base = n[n.index < start].iloc[-1:] if start else n.iloc[:1]
        sl = n[(n.index >= (start or n.index[0])) & (n.index <= (end or n.index[-1]))]
        nav = pd.concat([base, sl[~sl.index.isin(base.index)]])
        s = perf(nav)
        d = self.daily[(self.daily.index >= (start or self.daily.index[0])) & (self.daily.index <= (end or self.daily.index[-1]))]
        cost = float(d["fee"].sum() + d["tax"].sum())
        s.update({"turnover": float(d["turnover"].sum()), "turnover_ann": float(d["turnover"].mean() * TRADING_DAYS),
                  "cost": cost / float(nav.iloc[0]), "n_hold_min": int(d["n_hold"].min()), "n_hold_max": int(d["n_hold"].max()),
                  "cash_min": float(d["cash_ratio"].min()), "cash_max": float(d["cash_ratio"].max()),
                  "ap_min": float(d["ap_min"].min()), "ap_fail_days": int((d["ap_min"] < ACTIVE_SHARE_MIN).sum()),
                  "ap_max_consecutive": max_consecutive((d["ap_min"] < ACTIVE_SHARE_MIN).to_numpy()),
                  "viol": int(d[["v_holdings", "v_weight", "v_cash"]].to_numpy().sum()),
                  "stop_losses": int(d["n_stop"].sum()), "swaps": int(d["n_added"].sum()),
                  "blackout_days": int(d["blackout"].sum())})
        return s


def max_consecutive(mask) -> int:
    best = run = 0
    for x in mask:
        run = run + 1 if x else 0
        best = max(best, run)
    return best


def _proposal(cands: pd.DataFrame, held: dict, pp: ProxyParams) -> list[tuple[str, str]]:
    base = proxy_picks(cands, pp.n_picks - len(pp.fixed), pp.n_high)
    base = list(pp.fixed) + [(t, lv) for t, lv in base if t not in dict(pp.fixed)]
    if pp.sticky_levels:
        base = [(t, held[t]["level"] if t in held else lv) for t, lv in base]
    if not pp.keep_rank:
        return base
    keep = [t for t in cands.index if t in held and cands.at[t, "rank"] <= pp.keep_rank]
    order = keep + [t for t, _ in base if t not in keep]
    lv = dict(base)
    return [(t, lv.get(t) or held[t]["level"]) for t in order[:pp.n_picks]]


def run_conviction_backtest(panel: Panel, factors: dict[str, pd.DataFrame], caps: pd.DataFrame,
                            top10: dict | None, start: str | None = None, end: str | None = None, *,
                            params: ConvictionParams | None = None, cand_params: CandidateParams | None = None,
                            proxy: ProxyParams | None = None, events: list[dict] | None = None,
                            capital: float = INITIAL_CAPITAL, cost_free: bool = False) -> ConvictionBacktest:
    cp = params or ConvictionParams()
    pp = proxy or ProxyParams()
    dates = panel.dates
    first = MIN_HISTORY if start is None else max(MIN_HISTORY, next(i for i, d in enumerate(dates) if d >= start))
    last = len(dates) - 1 if end is None else max(i for i, d in enumerate(dates) if d <= end)
    led = Ledger(cash=float(capital), fee_rate=0.0 if cost_free else FEE_RATE, tax_rate=0.0 if cost_free else TAX_RATE)
    last_close: dict[str, float] = {}
    nav_pts = {dates[first - 1]: float(capital)}
    picks: dict[str, dict] = {}                      # ticker -> {level, entry}
    fillers: list[str] = []
    rows, unfilled, rejected_buys, build_errors, log = [], 0, 0, 0, []
    for i in range(first, last + 1):
        D, T = dates[i - 1], dates[i]
        for t, v in panel.close.loc[D].dropna().items():
            last_close[t] = float(v)
        for t, k in panel.split_ratios_on(T).items():
            if t in led.holdings:
                led.holdings[t] = int(round(led.holdings[t] * k))
            if t in last_close:
                last_close[t] /= k
        prev_nav = float(led.nav({t: last_close[t] for t in led.holdings})) if led.holdings else led.cash
        cur_w = {t: n * last_close[t] / prev_nav for t, n in led.holdings.items()}

        fdf = factors_on(factors, D)
        cap = caps.loc[D]
        elig = set(fdf.index[(fdf["adv20"] >= cp.min_adv) & cap.reindex(fdf.index).notna() & panel.close.loc[D].reindex(fdf.index).notna()])
        held = {t: HeldPick(v["level"], i - dates.index(v["entry"]), pick_return(panel, t, v["entry"], D))
                for t, v in picks.items() if t in led.holdings}
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        blk = bool(blackout_events(T, events, nxt)) if events is not None else bool(blackout_events(T, None, nxt))
        cands = screen(fdf, top10, cand_params, held=set(held))
        prop = _proposal(cands, {t: picks[t] for t in held}, pp)
        final, _, dnotes = discipline(prop, held, cp, blackout=blk, strict=False, eligible=elig)

        cur_ap = (check_target_weights(cur_w, top10, margin=0.0, expected_etfs=list(top10))["min"]
                  if top10 and cur_w else None)
        ap_target = cp.ap_recovery_target if cur_ap is not None and cur_ap < ACTIVE_SHARE_MIN else cp.ap_target
        forced = cur_ap is not None and cur_ap < cp.ap_trigger
        res = build_portfolio(final, cap, elig, top10, cp, current_fillers=fillers, ap_target=ap_target,
                              current=cur_w, no_new=blk and bool(cur_w),
                              exits=[t for t in held if t not in final])
        orders = []
        if res.errors:
            build_errors += 1
            log.append({"date": T, "build_errors": list(res.errors)})
            target = None
        else:
            target = res.weights
            if not forced and not guard_triggered(cur_w, cp):
                target, _ = apply_band(target, cur_w, cp, top10, ap_target)
            plan = plan_orders(target, prev_nav, led.cash, last_close, dict(led.holdings), buy_buffer=BUY_BUFFER)
            orders = plan.orders
        n_stop = sum(1 for n in dnotes if n.startswith("stop_loss"))
        added = [t for t in final if t not in held] if target is not None else []

        avg = panel.avg_price.loc[T]
        fills = []
        for o in orders:
            px = avg.get(o.ticker)
            if px is None or np.isnan(px):
                unfilled += 1
                continue
            fills.append({"ticker": o.ticker, "side": o.side, "shares": o.shares})
        avg_d = {o["ticker"]: float(avg[o["ticker"]]) for o in fills}
        rejected = _reject_unaffordable_buys(fills, avg_d, led.cash, led.fee_rate, led.tax_rate)
        fills = [o for o in fills if o not in rejected]
        rejected_buys += len(rejected)
        cost = led.apply_fills(fills, avg_d)
        traded = sum(o["shares"] * avg_d[o["ticker"]] for o in fills)

        if target is not None:                       # 更新名單狀態（新進者成本日 = T）
            picks = {t: {"level": lv, "entry": picks[t]["entry"] if t in picks and t in held else T}
                     for t, lv in final.items() if t in res.weights}
            fillers = list(res.fillers)
        picks = {t: v for t, v in picks.items() if t in led.holdings or v["entry"] == T}
        log.append({"date": T, "picks": dict((t, v["level"]) for t, v in picks.items()), "fillers": list(fillers)})

        for t, v in panel.close.loc[T].dropna().items():
            last_close[t] = float(v)
        nav = float(led.nav({t: last_close[t] for t in led.holdings}))
        w = {t: n * last_close[t] / nav for t, n in led.holdings.items()}
        cash_ratio = led.cash / nav
        ap_min = float("nan")
        if top10 and w:
            ap_min = float(check_target_weights(w, top10, margin=0.0, expected_etfs=list(top10))["min"])
        rows.append({"date": T, "nav": nav, "cash_ratio": cash_ratio, "n_hold": len(w),
                     "n_conv": sum(1 for t in picks if t in w), "max_w": max(w.values()) if w else 0.0,
                     "turnover": traded / 2 / prev_nav, "fee": cost["fee"], "tax": cost["tax"], "n_orders": len(fills),
                     "v_holdings": int(not (MIN_HOLDINGS <= len(w) <= MAX_HOLDINGS)),
                     "v_weight": int(any(x > max_weight(t) + 1e-12 for t, x in w.items())),
                     "v_weight_buy": int(any(w.get(o["ticker"], 0.0) > max_weight(o["ticker"]) + 1e-12
                                             for o in fills if o["side"] == "BUY")),
                     "v_cash": int(cash_ratio < 0 or cash_ratio >= CASH_MAX), "ap_min": ap_min,
                     "n_stop": n_stop, "n_added": len(added), "blackout": int(blk), "build_error": int(bool(res.errors))})
        nav_pts[T] = nav
    daily = pd.DataFrame(rows).set_index("date")
    viol = {"holdings": int(daily["v_holdings"].sum()), "weight": int(daily["v_weight"].sum()),
            "cash": int(daily["v_cash"].sum())}
    return ConvictionBacktest(pd.Series(nav_pts), daily, viol, unfilled, rejected_buys, build_errors, log)


def window_starts(dates: list[str], first: str, length: int, step: int, last: str | None = None) -> list[int]:
    lo = max(MIN_HISTORY, next(i for i, d in enumerate(dates) if d >= first))
    hi = (max(i for i, d in enumerate(dates) if d <= last) if last else len(dates) - 1) - length + 1
    return list(range(lo, hi + 1, step))


def window_stats(r: ConvictionBacktest, bench_cw: pd.Series, dates: list[str], s: int, length: int) -> dict:
    a, b = dates[s], dates[s + length - 1]
    sm = r.summary()
    cw = bench_cw[(bench_cw.index >= dates[s - 1]) & (bench_cw.index <= b)]
    cw_ret = float(cw.iloc[-1] / cw.iloc[0] - 1)
    return {"start": a, "end": b, "ret": sm["total_return"], "bench_cw_ret": cw_ret, "ex_cw": sm["total_return"] - cw_ret,
            "mdd": sm["mdd"], "bench_mdd": perf(cw)["mdd"], "turnover": sm["turnover"], "cost": sm["cost"],
            "viol": sm["viol"], "ap_min": sm["ap_min"], "ap_fail_days": sm["ap_fail_days"],
            "ap_max_consecutive": sm["ap_max_consecutive"], "stop_losses": sm["stop_losses"], "swaps": sm["swaps"],
            "n_hold_min": sm["n_hold_min"], "n_hold_max": sm["n_hold_max"], "cash_min": sm["cash_min"],
            "cash_max": sm["cash_max"], "build_errors": r.build_errors}


def excess_distribution(ex: pd.Series) -> dict:
    x = ex.dropna().to_numpy(dtype=float)
    if not len(x):
        return {"n": 0}
    q = np.quantile(x, [0.05, 0.25, 0.5, 0.75, 0.95])
    return {"n": int(len(x)), "mean": float(x.mean()), "std": float(x.std(ddof=1)) if len(x) > 1 else 0.0,
            "p5": float(q[0]), "p25": float(q[1]), "median": float(q[2]), "p75": float(q[3]), "p95": float(q[4]),
            "min": float(x.min()), "max": float(x.max()), "p_gt_0": float((x > 0).mean()),
            "p_gt_5": float((x > 0.05).mean()), "p_gt_10": float((x > 0.10).mean()), "p_lt_m10": float((x < -0.10).mean()),
            "p_lt_m5": float((x < -0.05).mean())}


def nan_to_none(x):
    return None if isinstance(x, float) and math.isnan(x) else x
