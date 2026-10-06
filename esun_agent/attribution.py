"""績效歸因：把策略相對 (b)（市值加權上限基準）的落後，沿一條「階梯」逐項拆開。

每一階都是一次完整回測（從起始日連續跑），只改一個因素；樣本外報酬的相鄰差即為該因素的貢獻，
全部加總恰好等於「策略 − (b)」（telescoping，沒有殘差）。

  P0  (b) 基準          150 檔市值加權上限、每日再平衡、100% 投資、無摩擦
  P1  只持核心 top-K    市值前 K 大（同核心規則）、100% 投資、每日再平衡、無摩擦      → (3) 核心涵蓋範圍
  P2  ＋衛星            核心 65% ＋ 衛星 35%、100% 投資、每日再平衡、無摩擦          → (4) 衛星選股
  P3  ＋現金 3%         投資比例 97%                                               → (5) 現金
  P4  ＋5 日再平衡/2% 帶 每 5 日決策、無交易帶 2%、無 Active Share 修正              → (6a) 再平衡頻率與無交易帶
  P5  ＋Active Share    加上 Active Share 修正（＝策略的目標權重）                    → (2) Active Share 修正
  P6  實際撮合・免費用   Ledger 回測：T−1 決策、T 日均價成交、整股、費稅 = 0           → (6b) 時間差（D 收盤 → T 均價）與整股
  P7  實際撮合・含費用   完整策略                                                    → (1) 費用

P0–P5 是「紙上組合」（權重 × 還原報酬、無費用、可分割股數）。P0 與 cap_weight_benchmark 逐日一致（有測試）。
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from .backtest import cap_weight_benchmark, run_backtest
from .data.prices import Panel
from .strategy.core_satellite import CoreSatellite, CoreSatelliteParams
from .strategy.factors import MIN_HISTORY, SAT_FACTOR_NAMES, eligible, factors_on
from .strategy.marketcap import capped_cap_weights
from .strategy.portfolio import PortfolioResult


def daily_returns(panel: Panel) -> pd.DataFrame:
    """還原價日報酬；前後兩日任一天沒有收盤價者記 0（與 cap_weight_benchmark 同一定義）。"""
    px = panel.adj_close.ffill(limit=10)
    return (px / px.shift(1) - 1).where(panel.close.notna() & panel.close.shift(1).notna(), 0.0).fillna(0.0)


class BenchmarkDecider:
    """P0：(b) 本身（150 檔、市值加權、2330 ≤ 20%、其他 ≤ 8%）。"""
    rebalance_every = 1
    min_adv = 0.0

    def decide(self, fdf, cap, current, top10):
        w = capped_cap_weights(cap.dropna())
        return PortfolioResult(weights=w, selected=list(w), cash=0.0)


def paper_nav(panel: Panel, factors, caps: pd.DataFrame, strat, top10=None,
              first: int = MIN_HISTORY) -> tuple[pd.Series, dict]:
    """紙上組合（無費用、無整股）：D 日收盤決定權重（只用 D 日以前資料），T 日賺 D 收盤→T 收盤的還原報酬；
    非再平衡日權重隨報酬漂移。回傳 (NAV 序列（起點 1.0）, {T: PortfolioResult})。"""
    dates = panel.dates
    ret = daily_returns(panel)
    nav, vals = 1.0, {}
    out, log = {dates[first - 1]: 1.0}, {}
    for i in range(first, len(dates)):
        D, T = dates[i - 1], dates[i]
        if (i - first) % strat.rebalance_every == 0:
            cur = {t: v / nav for t, v in vals.items()}
            res = strat.decide(factors_on(factors, D), caps.loc[D], cur, top10)
            tot = sum(res.weights.values())
            norm = 1.0 if tot <= 1.0 + 1e-9 else tot
            vals = {t: w / norm * nav for t, w in res.weights.items() if w > 0}
            log[T] = res
        cash = nav - sum(vals.values())
        vals = {t: v * (1.0 + ret.at[T, t]) for t, v in vals.items()}
        nav = cash + sum(vals.values())
        out[T] = nav
    return pd.Series(out), log


def oos_return(nav: pd.Series, start: str) -> float:
    """start 之後（含）的累計報酬，起點 = start 前最後一個 NAV。"""
    base = nav[nav.index < start].iloc[-1]
    return float(nav.iloc[-1] / base - 1)


LADDER = [
    ("P0", "(b) 基準", None),
    ("P1", "(3) 核心只持前 K 大（vs 150 檔）", "coverage"),
    ("P2", "(4) 加衛星 35%（選股＋核心降到 65%）", "satellite"),
    ("P3", "(5) 現金 3%", "cash"),
    ("P4", "(6a) 每 5 日再平衡＋2% 無交易帶", "rebalance"),
    ("P5", "(2) Active Share 修正", "active_share"),
    ("P6", "(6b) 時間差：D 收盤決策、T 日均價成交、整股", "execution"),
    ("P7", "(1) 費用（手續費＋證交稅）", "costs"),
]


def attribution_ladder(panel: Panel, factors, caps: pd.DataFrame, top10, params: CoreSatelliteParams,
                       oos_start: str, strategy_name: str = "strategy") -> dict:
    """回傳 {"rungs": [{code, label, nav, oos, step}], "total": 策略 − (b), "sat": 衛星分析, "navs": {code: Series}}。"""
    ideal = replace(params, rebalance_every=1, band=0.0, cash_target=0.0)
    navs: dict[str, pd.Series] = {}
    navs["P0"], _ = paper_nav(panel, factors, caps, BenchmarkDecider())
    navs["P1"], _ = paper_nav(panel, factors, caps, CoreSatellite("P1", replace(ideal, sat_n=0, core_share=1.0)))
    navs["P2"], log2 = paper_nav(panel, factors, caps, CoreSatellite("P2", ideal))
    navs["P3"], _ = paper_nav(panel, factors, caps, CoreSatellite("P3", replace(ideal, cash_target=params.cash_target)))
    p4 = replace(params)
    navs["P4"], _ = paper_nav(panel, factors, caps, CoreSatellite("P4", p4), top10=None)
    navs["P5"], _ = paper_nav(panel, factors, caps, CoreSatellite("P5", p4), top10=top10)
    strat = CoreSatellite(strategy_name, params)
    navs["P6"] = run_backtest(panel, strat, factors, top10, caps=caps, cost_free=True).nav
    navs["P7"] = run_backtest(panel, strat, factors, top10, caps=caps).nav
    rungs, prev = [], None
    for code, label, _ in LADDER:
        r = oos_return(navs[code], oos_start)
        rungs.append({"code": code, "label": label, "oos": r, "step": None if prev is None else r - prev})
        prev = r
    return {"rungs": rungs, "total": rungs[-1]["oos"] - rungs[0]["oos"], "navs": navs,
            "sat": sleeve_analysis(panel, factors, caps, log2, params, oos_start)}


def sleeve_analysis(panel: Panel, factors, caps, log: dict, params: CoreSatelliteParams, oos_start: str) -> dict:
    """用「每日再平衡、無摩擦」的 P2 紙上組合，拆出樣本外的核心、衛星兩個部位的報酬，
    並與「衛星候選池等權」（核心以外、因子齊全且流動性合格的全部股票）比較，看衛星選股有沒有勝過隨便選。"""
    ret = daily_returns(panel)
    core_r, sat_r, pool_r, idx = [], [], [], []
    for T, res in log.items():
        if T < oos_start:
            continue
        D = panel.dates[panel.dates.index(T) - 1]
        core, sat = res.selected[:params.core_k], res.selected[params.core_k:]
        fdf = factors_on(factors, D)
        pool = list(eligible(fdf, params.min_adv, SAT_FACTOR_NAMES).difference(core))

        def wret(names):
            w = pd.Series({t: res.weights.get(t, 0.0) for t in names})
            return float((w * ret.loc[T, names]).sum() / w.sum()) if len(names) and w.sum() > 0 else 0.0
        core_r.append(wret(core)); sat_r.append(wret(sat))
        pool_r.append(float(ret.loc[T, pool].mean()) if pool else 0.0)
        idx.append(T)
    comp = lambda x: float(np.prod(1 + np.array(x)) - 1)       # noqa: E731
    return {"days": len(idx), "core": comp(core_r), "sat": comp(sat_r), "pool_ew": comp(pool_r)}
