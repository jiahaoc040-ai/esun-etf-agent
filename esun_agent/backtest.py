"""回測：用 Ledger 逐日模擬正式流程。

時序（T = 交易日，D = T 的前一交易日）：
  - T 日的決策只使用 D 日收盤後的資料（因子、前日淨值、前日收盤價、現有持股）。
  - 委託由 derive_orders 機械產生（目標股數 = floor(權重 × D 日 NAV ÷ D 日收盤價 ÷ 1000) × 1000）。
  - 以 T 日「原始」成交均價（avg_price）成交，扣手續費與證交稅；NAV 用 T 日原始收盤價。
  - 因子與基準報酬用還原價；若持股在 T 日發生分割/減資，持股股數與 D 日收盤價先換成 T 日的股數口徑
    （NAV 不變），下單與成交仍是原始價格口徑。
  - T 日沒有成交價（停牌）的委託視為未成交；估值沿用最近收盤價。
假設與限制：不含股利（官方除息現金期末才加回）、無市場衝擊、成交一律以均價成交。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import FEE_RATE, TAX_RATE, INITIAL_CAPITAL, MAX_HOLDINGS, MIN_HOLDINGS, CASH_MAX, max_weight
from .data.etf_holdings import check_target_weights
from .data.prices import Panel
from .ledger import Ledger
from .orders import derive_orders
from .strategy.factors import MIN_HISTORY, compute_factors, eligible, factors_on, score
from .strategy.marketcap import capped_cap_weights, market_cap
from .strategy.portfolio import PortfolioParams, construct_portfolio

TRADING_DAYS = 252


@dataclass
class Strategy:
    """純因子選股策略（PR #3 初版，每日再平衡）。run_backtest 只依賴 name / rebalance_every / min_adv / decide。"""
    name: str
    factor_weights: dict[str, float]
    params: PortfolioParams = field(default_factory=PortfolioParams)
    min_adv: float = 2e8            # 20 日均成交值門檻（單檔約 4% × 10 億 = 4000 萬，≈ 日成交值的 20% 以內）
    rebalance_every: int = 1

    def decide(self, fdf, cap, current, top10):
        sc = score(fdf, self.factor_weights, eligible(fdf, self.min_adv))
        return construct_portfolio(sc, current, self.params, top10)


@dataclass
class BacktestResult:
    nav: pd.Series                  # index = 日期；第一筆是起始資金（首個交易日的前一日）
    daily: pd.DataFrame
    violations: dict[str, int]
    unfilled: int = 0               # T 日無成交價（停牌）而未成交的委託數
    rejected_buys: int = 0          # 賣單未成交導致現金不足而被拒絕的買單數

    def period(self, start: str | None = None, end: str | None = None) -> pd.Series:
        n = self.nav
        base = n[n.index < start].iloc[-1:] if start else n.iloc[:1]
        sl = n[(n.index >= (start or n.index[0])) & (n.index <= (end or n.index[-1]))]
        return pd.concat([base, sl[~sl.index.isin(base.index)]])

    def summary(self, start: str | None = None, end: str | None = None) -> dict:
        s = perf(self.period(start, end))
        d = self.daily
        d = d[(d.index >= (start or d.index[0])) & (d.index <= (end or d.index[-1]))]
        s["turnover_ann"] = float(d["turnover"].mean() * TRADING_DAYS) if len(d) else math.nan
        s["avg_holdings"] = float(d["n_hold"].mean()) if len(d) else math.nan
        s["viol_holdings"] = int(d["v_holdings"].sum())
        s["viol_weight"] = int(d["v_weight"].sum())
        s["viol_cash"] = int(d["v_cash"].sum())
        s["ap_fail_days"] = int((~d["ap_ok"]).sum())
        s["ap_min"] = float(d["ap_min"].min()) if len(d) else math.nan
        costs = float(d["fee"].sum() + d["tax"].sum())
        nav = self.period(start, end)
        pnl = float(nav.iloc[-1] - nav.iloc[0])
        s["cost_total"] = costs
        s["cost_drag_ann"] = costs / float(nav.mean()) * TRADING_DAYS / max(len(d), 1) if len(d) else math.nan
        s["cost_share_of_gross"] = costs / (pnl + costs) if pnl > 0 else math.nan   # 費用占「毛利」比例
        return s


def perf(nav: pd.Series) -> dict:
    """nav 的第一筆當成起點（不計報酬）。"""
    r = nav.pct_change().dropna()
    if r.empty:
        return {"days": 0, "total_return": 0.0, "ann_return": 0.0, "ann_vol": 0.0, "sharpe": math.nan, "mdd": 0.0}
    total = float(nav.iloc[-1] / nav.iloc[0] - 1)
    vol = float(r.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(r) > 1 else math.nan
    ann = float((1 + total) ** (TRADING_DAYS / len(r)) - 1)
    mdd = float((nav / nav.cummax() - 1).min())
    return {"days": len(r), "total_return": total, "ann_return": ann, "ann_vol": vol,
            "sharpe": float(ann / vol) if vol and not math.isnan(vol) else math.nan, "mdd": mdd}


def equal_weight_benchmark(panel: Panel, start: str | None = None, end: str | None = None) -> pd.Series:
    """名單內「前後兩日皆有價」股票的等權、每日再平衡報酬（還原價），起點 = 1.0。"""
    px = panel.adj_close
    r = (px / px.shift(1) - 1).mean(axis=1, skipna=True).fillna(0.0)
    nav = (1 + r).cumprod()
    lo = start or nav.index[0]
    base = nav[nav.index < lo].iloc[-1:] if (nav.index < lo).any() else nav.iloc[:1]
    out = pd.concat([base, nav[(nav.index >= lo) & (nav.index <= (end or nav.index[-1]))]])
    return out[~out.index.duplicated()]


def _reject_unaffordable_buys(fills: list[dict], avg: dict[str, float], cash: float,
                              fee_rate: float = FEE_RATE, tax_rate: float = TAX_RATE) -> list[dict]:
    """模擬券商「餘額不足拒絕委託」：賣出先入帳，若買進後現金 < 0，由金額最大的買單開始拒絕，直到不透支。"""
    sells = sum(o["shares"] * avg[o["ticker"]] * (1 - fee_rate - tax_rate) for o in fills if o["side"] == "SELL")
    buys = sorted((o for o in fills if o["side"] == "BUY"), key=lambda o: -o["shares"] * avg[o["ticker"]])
    need = sum(o["shares"] * avg[o["ticker"]] * (1 + fee_rate) for o in buys)
    rejected = []
    for o in buys:
        if cash + sells - need >= 0:
            break
        need -= o["shares"] * avg[o["ticker"]] * (1 + fee_rate)
        rejected.append(o)
    return rejected


def cap_weight_benchmark(panel: Panel, caps: pd.DataFrame, start: str | None = None, end: str | None = None,
                         cap_tsmc: float = 0.20, cap_other: float = 0.08) -> pd.Series:
    """市值加權並套上限（2330 ≤ 20%、其他 ≤ 8%）的基準：每日以「前一日」市值決定權重（只用前日資料）、
    當日還原報酬加權、每日再平衡、不計成本。權重只放在前一日有市值的股票，起點 = 1.0。"""
    px = panel.adj_close.ffill(limit=10)
    ret = (px / px.shift(1) - 1).where(panel.close.notna() & panel.close.shift(1).notna(), 0.0).fillna(0.0)
    dates = panel.dates
    out = {dates[0]: 1.0}
    nav = 1.0
    for i in range(1, len(dates)):
        w = capped_cap_weights(caps.loc[dates[i - 1]], cap_tsmc, cap_other)
        nav *= 1.0 + sum(w[t] * ret.at[dates[i], t] for t in w) / max(sum(w.values()), 1e-12)
        out[dates[i]] = nav
    nav = pd.Series(out)
    lo = start or nav.index[0]
    base = nav[nav.index < lo].iloc[-1:] if (nav.index < lo).any() else nav.iloc[:1]
    out = pd.concat([base, nav[(nav.index >= lo) & (nav.index <= (end or nav.index[-1]))]])
    return out[~out.index.duplicated()]


def run_backtest(panel: Panel, strat, factors: dict[str, pd.DataFrame] | None = None,
                 top10: dict[str, dict[str, float]] | None = None, start: str | None = None,
                 end: str | None = None, capital: float = INITIAL_CAPITAL,
                 caps: pd.DataFrame | None = None, cost_free: bool = False, tilt_fn=None) -> BacktestResult:
    """start/end 為「交易日 T」的範圍。第一個可交易日至少是第 MIN_HISTORY+1 個資料日（因子暖機）。"""
    factors = factors if factors is not None else compute_factors(panel)
    caps = caps if caps is not None else market_cap(panel)[0]
    dates = panel.dates
    first = MIN_HISTORY if start is None else max(MIN_HISTORY, next(i for i, d in enumerate(dates) if d >= start))
    last = len(dates) - 1 if end is None else max(i for i, d in enumerate(dates) if d <= end)
    top10_ap = top10 or None

    led = Ledger(cash=float(capital), fee_rate=0.0 if cost_free else FEE_RATE, tax_rate=0.0 if cost_free else TAX_RATE)
    last_close: dict[str, float] = {}
    nav_pts = {dates[first - 1]: float(capital)}
    rows, unfilled, rejected_buys = [], 0, 0
    last_tilts = None
    for i in range(first, last + 1):
        D, T = dates[i - 1], dates[i]
        # --- D 日收盤狀態（先把 D 日收盤價載入）
        for t, v in panel.close.loc[D].dropna().items():
            last_close[t] = float(v)
        # --- T 日若有分割/減資：持股與 D 日收盤價換成 T 日口徑
        for t, k in panel.split_ratios_on(T).items():
            if t in led.holdings:
                led.holdings[t] = int(round(led.holdings[t] * k))
            if t in last_close:
                last_close[t] /= k
        prev_nav = led.nav({t: last_close[t] for t in led.holdings}) if led.holdings else led.cash
        prev_nav = float(prev_nav)
        cur_w = {t: n * last_close[t] / prev_nav for t, n in led.holdings.items()}

        # --- 決策（只用 D 日以前資料）；非再平衡日不產生委託（D-Plan 上全部是 no_trade）
        orders, rebalanced = [], False
        guard = getattr(strat, "force_rebalance", None)
        tilts, tilts_changed = None, False
        if tilt_fn is not None:                     # Agent（或隨機）加減碼：tilt 有變動的當天可以交易
            fd = factors_on(factors, D)
            elig = list(fd.index[(fd["adv20"] >= strat.min_adv) & caps.loc[D].reindex(fd.index).notna()])
            tilts = tilt_fn(D, elig, cur_w)
            tilts_changed = tilts != last_tilts
            last_tilts = tilts
        if ((i - first) % strat.rebalance_every == 0 or tilts_changed
                or (guard is not None and guard(cur_w, top10_ap))):
            rebalanced = True
            fdf = factors_on(factors, D)
            res = (strat.decide(fdf, caps.loc[D], cur_w, top10_ap, tilts) if tilt_fn is not None
                   else strat.decide(fdf, caps.loc[D], cur_w, top10_ap))
            tickers = sorted(set(res.weights) | set(led.holdings))
            decisions = [{"ticker": t, "target_weight": res.weights.get(t, 0.0), "decision_id": f"D{n}"}
                         for n, t in enumerate(tickers, 1)]
            orders = derive_orders(decisions, prev_nav, last_close, dict(led.holdings))

        # --- T 日成交
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

        # --- T 日收盤估值（原始價）
        for t, v in panel.close.loc[T].dropna().items():
            last_close[t] = float(v)
        nav = float(led.nav({t: last_close[t] for t in led.holdings}))
        w = {t: n * last_close[t] / nav for t, n in led.holdings.items()}
        cash_ratio = led.cash / nav
        ap_ok, ap_min = True, float("nan")
        if top10_ap and w:
            ap = check_target_weights(w, top10_ap, margin=0.0, expected_etfs=list(top10_ap))
            ap_ok, ap_min = bool(ap["ok"]), float(ap["min"])
        rows.append({
            "date": T, "nav": nav, "cash_ratio": cash_ratio, "n_hold": len(w),
            "max_w": max(w.values()) if w else 0.0, "tsmc_w": w.get("2330", 0.0),
            "turnover": traded / 2 / prev_nav, "fee": cost["fee"], "tax": cost["tax"], "n_orders": len(fills),
            "v_holdings": int(not (MIN_HOLDINGS <= len(w) <= MAX_HOLDINGS)),
            "v_weight": int(any(x > max_weight(t) + 1e-12 for t, x in w.items())),
            "v_cash": int(cash_ratio < 0 or cash_ratio >= CASH_MAX), "ap_ok": ap_ok, "ap_min": ap_min,
            "rebalanced": rebalanced,
        })
        nav_pts[T] = nav
    daily = pd.DataFrame(rows).set_index("date")
    viol = {"holdings": int(daily["v_holdings"].sum()), "weight": int(daily["v_weight"].sum()),
            "cash": int(daily["v_cash"].sum())}
    return BacktestResult(nav=pd.Series(nav_pts), daily=daily, violations=viol, unfilled=unfilled,
                          rejected_buys=rejected_buys)


def window_returns(panel: Panel, strat, factors, top10, length: int = 24, step: int = 5,
                   first: str | None = None, last: str | None = None, caps: pd.DataFrame | None = None,
                   bench_cw: pd.Series | None = None) -> pd.DataFrame:
    """每隔 step 個交易日起算一段 length 個交易日的全新回測（從 10 億現金起、含建倉成本），
    回傳每段的報酬、MDD、兩個基準報酬與超額報酬（ex_ew：相對 150 檔等權；ex_cw：相對市值加權上限基準）。
    first/last 是窗口「起點 T」的下限、「終點 T」的上限。"""
    caps = caps if caps is not None else market_cap(panel)[0]
    if bench_cw is None:
        bench_cw = cap_weight_benchmark(panel, caps)
    dates = panel.dates
    lo = max(MIN_HISTORY, next(i for i, d in enumerate(dates) if d >= first) if first else MIN_HISTORY)
    hi = (max(i for i, d in enumerate(dates) if d <= last) if last else len(dates) - 1) - length + 1
    out = []
    for s in range(lo, hi + 1, step):
        a, b = dates[s], dates[s + length - 1]
        r = run_backtest(panel, strat, factors, top10, start=a, end=b, caps=caps)
        p = perf(r.nav)
        ew = equal_weight_benchmark(panel, a, b)
        cw = bench_cw[(bench_cw.index >= dates[s - 1]) & (bench_cw.index <= b)]
        ew_ret = float(ew.iloc[-1] / ew.iloc[0] - 1)
        cw_ret = float(cw.iloc[-1] / cw.iloc[0] - 1)
        out.append({"start": a, "end": b, "ret": p["total_return"], "mdd": p["mdd"], "bench_ret": ew_ret,
                    "bench_cw_ret": cw_ret, "ex_ew": p["total_return"] - ew_ret, "ex_cw": p["total_return"] - cw_ret,
                    "viol": sum(r.violations.values())})
    return pd.DataFrame(out)
