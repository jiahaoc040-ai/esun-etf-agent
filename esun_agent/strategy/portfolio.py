"""因子分數 → 目標權重（22–28 檔）。

流程（construct_portfolio）：
  1. select_holdings  取分數前 n_hold 名；現有持股只要排名在 n_hold+keep_rank_buffer 內就保留（降低換手），
                      總檔數限制在 [min_hold, max_hold]（預設 22–28，比官方 20–30 留緩衝）。
  2. raw_weights      2330 另外處理（若入選給固定 tsmc_weight，上限 25%−緩衝）；其餘依分數傾斜，
                      每檔上限 min(soft_cap, 10%−緩衝)，現金目標 cash_target ∈ [1%, 22%]。
  3. apply_no_trade_band  既有持股的目標與現權重差 < band（預設 1.5%）→ 不動（沿用現權重），
                      剩餘差額由其他名稱吸收，讓現金仍落在 [1%, 22%]。新進場與完全出場不受無交易帶限制。
  4. fit_active_share 用 check_target_weights 檢查 Active Share，不通過就壓低重疊最大的標的並把權重分給不重疊者。
權重均為占 NAV 的比例，現金 = 1 − Σ權重。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..config import CASH_BUFFER_HIGH, CASH_BUFFER_LOW, TSMC, WEIGHT_BUFFER, max_weight
from ..data.etf_holdings import check_target_weights


@dataclass(frozen=True)
class PortfolioParams:
    n_hold: int = 25
    min_hold: int = 22
    max_hold: int = 28
    keep_rank_buffer: int = 3          # 現有持股排名 ≤ n_hold + 此值就保留
    tsmc_weight: float = 0.12          # 2330 入選時的固定權重
    tilt: float = 0.25                 # 權重 ∝ clip(1 + tilt × score, 0.5, 2.0)
    soft_cap: float = 0.07             # 一般個股的策略上限（再低於官方 10% − 緩衝）
    cash_target: float = 0.03
    cash_low: float = CASH_BUFFER_LOW
    cash_high: float = CASH_BUFFER_HIGH
    band: float = 0.015                # 無交易帶
    ap_margin: float = 0.02            # Active Share 緩衝（要求 ≥ 20% + margin）
    ap_max_iter: int = 40


def cap_of(ticker: str, p: PortfolioParams) -> float:
    """策略權重上限 = 官方上限 − 緩衝（一般個股再受 soft_cap 限制）。"""
    hard = max_weight(ticker) - WEIGHT_BUFFER
    return hard if ticker == TSMC else min(hard, p.soft_cap)


# --------------------------------------------------------------------------- 1. 選股

def select_holdings(scores: pd.Series, current: set[str], p: PortfolioParams) -> list[str]:
    """scores 已排序（高→低）且只含合格股票。回傳持股清單（長度 ∈ [min_hold, max_hold]，除非合格股票不足）。"""
    rank = {t: i for i, t in enumerate(scores.index)}
    keep = [t for t in scores.index if t in current and rank[t] < p.n_hold + p.keep_rank_buffer]
    keep = keep[:p.max_hold]
    target_n = max(p.min_hold, min(p.n_hold, p.max_hold))
    chosen = list(keep)
    for t in scores.index:
        if len(chosen) >= target_n:
            break
        if t not in chosen:
            chosen.append(t)
    if len(chosen) > p.max_hold:
        chosen = chosen[:p.max_hold]
    return sorted(chosen, key=lambda t: rank[t])


# --------------------------------------------------------------------------- 2. 權重

def _waterfill(raw: dict[str, float], budget: float, caps: dict[str, float]) -> dict[str, float]:
    """依 raw 比例分配 budget，超過 cap 的釘在 cap，剩餘重新分配。cap 總和不足 budget 時全部釘 cap。"""
    w, free = {}, dict(raw)
    while free:
        tot = sum(free.values())
        rem = budget - sum(w.values())
        over = {t for t, r in free.items() if rem * r / tot > caps[t] + 1e-12}
        if not over:
            w.update({t: rem * r / tot for t, r in free.items()})
            break
        for t in over:
            w[t] = caps[t]
            del free[t]
    return w


def raw_weights(selected: list[str], scores: pd.Series, p: PortfolioParams) -> dict[str, float]:
    equity = 1.0 - p.cash_target
    w: dict[str, float] = {}
    rest = [t for t in selected if t != TSMC]
    if TSMC in selected:
        w[TSMC] = min(p.tsmc_weight, cap_of(TSMC, p))
    raw = {t: min(2.0, max(0.5, 1.0 + p.tilt * float(scores[t]))) for t in rest}
    w.update(_waterfill(raw, equity - sum(w.values()), {t: cap_of(t, p) for t in rest}))
    return w


# --------------------------------------------------------------------------- 3. 無交易帶

def apply_no_trade_band(target: dict[str, float], current: dict[str, float],
                        p: PortfolioParams) -> dict[str, float]:
    """既有持股且目標>0、|目標−現權重| < band → 沿用現權重（不產生委託）。
    例外：現權重已超過該檔上限者一律回到目標。鎖定造成的現金缺口由未鎖定名稱按比例吸收；
    若吸收後現金仍超出 [cash_low, cash_high]，放棄無交易帶（回傳原目標）。"""
    locked = {t for t, w in target.items()
              if w > 0 and t in current and abs(w - current[t]) < p.band and current[t] <= cap_of(t, p)}
    if not locked:
        return dict(target)
    out = dict(target)
    for t in locked:
        out[t] = current[t]
    gap = sum(target.values()) - sum(out.values())     # >0：鎖定使股票部位偏低，需由自由名稱補足
    free = {t: w for t, w in out.items() if t not in locked and w > 0}
    if abs(gap) > 1e-12:
        if not free:
            return dict(target)
        scale = 1.0 + gap / sum(free.values())          # 按比例放大／縮小自由名稱
        cand = {t: w * scale for t, w in free.items()}
        if scale <= 0 or any(w > cap_of(t, p) + 1e-9 for t, w in cand.items()):
            return dict(target)
        out.update(cand)
    cash = 1.0 - sum(out.values())
    if not (p.cash_low - 1e-9 <= cash <= p.cash_high + 1e-9):
        return dict(target)
    return out


# --------------------------------------------------------------------------- 4. Active Share

def fit_active_share(weights: dict[str, float], scores: pd.Series, top10: dict[str, dict[str, float]] | None,
                     p: PortfolioParams) -> tuple[dict[str, float], dict | None]:
    """不通過（含 margin）時，把重疊最大的標的權重乘 0.8，釋出的權重分給「不在失敗 ETF 前 10 大」的名稱
    （依現權重比例、不超過上限）。回傳 (權重, 最後一次檢查結果)。top10 為 None 時不檢查。"""
    if not top10:
        return weights, None
    w = dict(weights)
    res = check_target_weights(w, top10, margin=p.ap_margin, expected_etfs=list(top10))
    for _ in range(p.ap_max_iter):
        bad = res["fails"] + res["warnings"]
        if not bad:
            break
        worst = min(bad, key=lambda x: x["min"])
        ov = [o["ticker"] for o in worst["overlap_tickers"]]
        if not ov:
            break
        victim = next((t for t in ov if w.get(t, 0) > 0), None)
        if victim is None:
            break
        cut = w[victim] * 0.2
        w[victim] -= cut
        recv = {t: x for t, x in w.items() if t not in ov and x > 0 and x < cap_of(t, p) - 1e-9}
        if not recv:
            w[victim] += cut
            break
        tot = sum(recv.values())
        spill = 0.0
        for t, x in recv.items():
            add = min(cut * x / tot, cap_of(t, p) - x)
            w[t] = x + add
            spill += add
        w[victim] += cut - spill  # 沒人吸收得下的部分留在原標的（避免現金暴增）
        res = check_target_weights(w, top10, margin=p.ap_margin, expected_etfs=list(top10))
    return w, res


def fit_active_share_minimal(weights: dict[str, float], top10: dict[str, dict[str, float]] | None,
                             p: PortfolioParams, max_names: int = 3, pool_from: int = 10, pool_to: int = 28,
                             floor_ratio: float = 0.5) -> tuple[dict[str, float], dict | None]:
    """最小 Active Share 調整：只動「造成重疊最大」的 1–3 檔（max_names），每次把該檔權重乘 0.8，
    釋出的權重分給權重排名第 pool_from+1～pool_to 名（預設第 11–28 名、且不在失敗 ETF 前 10 大）的標的，
    而不是像 fit_active_share 那樣讓所有名單外標的一起吸收。單檔最多降到原權重的 floor_ratio；
    已動過的檔數達上限、且它們都到下限時就停止（可能仍未通過，呼叫端看回傳的檢查結果）。"""
    if not top10:
        return weights, None
    w = dict(weights)
    orig = dict(weights)
    order = sorted(w, key=lambda t: (-w[t], t))
    pool = order[pool_from:pool_to]
    touched: list[str] = []
    res = check_target_weights(w, top10, margin=p.ap_margin, expected_etfs=list(top10))
    for _ in range(p.ap_max_iter):
        bad = res["fails"] + res["warnings"]
        if not bad:
            break
        worst = min(bad, key=lambda x: x["min"])
        ov = [o["ticker"] for o in worst["overlap_tickers"]]
        alive = lambda t: w.get(t, 0) > floor_ratio * orig.get(t, 0) + 1e-12      # noqa: E731
        victim = next((t for t in ov if t in touched and alive(t)), None)
        if victim is None and len(touched) < max_names:
            victim = next((t for t in ov if t not in touched and w.get(t, 0) > 0), None)
        if victim is None:
            break
        if victim not in touched:
            touched.append(victim)
        cut = min(w[victim] * 0.2, w[victim] - floor_ratio * orig[victim])
        recv = {t: w[t] for t in pool if t not in ov and t not in touched and 0 < w[t] < cap_of(t, p) - 1e-9}
        if not recv:
            break
        tot, spill = sum(recv.values()), 0.0
        for t, x in recv.items():
            add = min(cut * x / tot, cap_of(t, p) - x)
            w[t] = x + add
            spill += add
        w[victim] -= spill                    # 只扣掉真的被吸收的部分，現金不因此暴增
        res = check_target_weights(w, top10, margin=p.ap_margin, expected_etfs=list(top10))
    return w, res


# --------------------------------------------------------------------------- 總成

@dataclass
class PortfolioResult:
    weights: dict[str, float]
    selected: list[str]
    cash: float
    active_share: dict | None = None
    notes: list[str] = field(default_factory=list)


def finalize_weights(target: dict[str, float], current: dict[str, float], p: PortfolioParams,
                     top10: dict[str, dict[str, float]] | None, selected: list[str],
                     minimal_ap: bool = False) -> PortfolioResult:
    """目標權重 → Active Share 修正 → 無交易帶（若讓 Active Share 不過則放棄無交易帶）。
    minimal_ap=True 時用 fit_active_share_minimal（只動 1–3 檔），否則用 fit_active_share（全面壓低重疊）。"""
    if minimal_ap:
        target, ap = fit_active_share_minimal(target, top10, p)
    else:
        target, ap = fit_active_share(target, None, top10, p)
    banded = apply_no_trade_band(target, current, p)
    notes = []
    if banded != target:
        # 無交易帶可能讓 Active Share 變動，重新檢查；失敗就放棄無交易帶
        ap2 = check_target_weights(banded, top10, margin=p.ap_margin, expected_etfs=list(top10)) if top10 else None
        if ap2 is None or ap2["ok"]:
            target, ap = banded, ap2
        else:
            notes.append("band_dropped_for_active_share")
    return PortfolioResult(weights={t: w for t, w in target.items() if w > 0}, selected=selected,
                           cash=1.0 - sum(target.values()), active_share=ap, notes=notes)


def construct_portfolio(scores: pd.Series, current: dict[str, float], p: PortfolioParams,
                        top10: dict[str, dict[str, float]] | None = None) -> PortfolioResult:
    """scores：合格股票的分數（高→低）；current：現持股占 NAV 權重（以前日收盤計）。"""
    scores = scores.sort_values(ascending=False)
    if len(scores) < p.min_hold:
        raise ValueError(f"合格股票只有 {len(scores)} 檔，少於 min_hold={p.min_hold}")
    selected = select_holdings(scores, set(current), p)
    return finalize_weights(raw_weights(selected, scores, p), current, p, top10, selected)
