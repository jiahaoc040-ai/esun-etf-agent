"""正式策略：合規基準 A ＋ Agent 小幅加減碼（tilt）。

  base_weights(date)   A 的權重：合格名單（有 D 日價格、20 日均成交值 ≥ min_adv）的市值加權，2330 ≤ 20%、其他 ≤ 8%，
                       取權重最大的前 28 檔重新歸一，現金目標 3%；再做 Active Share 修正到 ≥ 25%（官方 20%），
                       修正順序：B 的最小調整法（只動 1–3 檔）→ 放寬到 6 檔 → 全面壓低（見 ensure_active_share）。
  apply_tilts(base, tilts)   tilts = {ticker: delta（占 NAV）}；單檔 |delta| ≤ 3%、主動偏離 Σ|delta|/2 ≤ 15%；
                       可加入 150 檔內不在前 28 的股票，持股維持 20–30 檔（內部上限 29）。套用後重新檢查：單檔上限、現金 [1%, 22%]、
                       Active Share ≥ 25%；超出時依序縮放／截斷／修正，並把結果記在 notes。
  decide / force_rebalance   回測與每日流程用：每 5 個交易日再平衡、無交易帶 2%；但 tilt 與前一日不同，或漂移保護
                       （權重逼近官方上限、Active Share 逼近 20%）觸發時，當天就可以交易。

權重皆為占 NAV 的比例，現金 = 1 − Σ權重。基準、tilt 都只用 D 日以前的資料。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from ..config import ACTIVE_SHARE_MIN, CASH_BUFFER_HIGH, CASH_BUFFER_LOW, TSMC
from ..data.etf_holdings import check_target_weights
from ..universe import load_universe
from .marketcap import capped_cap_weights
from .portfolio import (PortfolioParams, PortfolioResult, _waterfill, apply_no_trade_band, cap_of,
                        fit_active_share, fit_active_share_minimal)


class TiltError(ValueError):
    """tilt 不合法。errors 為逐條訊息，T4 可原樣回饋給 LLM 重試。"""

    def __init__(self, errors: list[str]):
        super().__init__("；".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class FinalParams:
    n_base: int = 28
    cash_target: float = 0.03
    cash_low: float = CASH_BUFFER_LOW
    cash_high: float = CASH_BUFFER_HIGH
    base_cap_tsmc: float = 0.20            # (b) 的單檔上限
    base_cap_other: float = 0.08
    tilt_cap_tsmc: float = 0.22            # tilt 之後的單檔上限（官方 25%／10%，留漂移空間）
    tilt_cap_other: float = 0.085
    max_delta: float = 0.03                # 單檔 |delta|
    max_active: float = 0.15               # Σ|delta| / 2
    min_hold: int = 20
    max_hold: int = 29                     # 官方 30：留 1 檔給「停牌賣不掉」的部位（賣單當天沒成交價，持股數會暫時多 1）
    ap_target: float = 0.25                # Active Share 目標（官方門檻 0.20）
    ap_guard: float = 0.04                 # 前日收盤 Active Share < 20% + ap_guard 就提前再平衡
    guard_tsmc: float = 0.24               # 前日收盤權重超過這些值就提前再平衡（官方 25%／10%）
    guard_other: float = 0.09
    band: float = 0.02
    rebalance_every: int = 5
    min_adv: float = 2e8
    ap_max_iter: int = 60

    def portfolio_params(self) -> PortfolioParams:
        return PortfolioParams(n_hold=self.n_base, min_hold=self.min_hold, max_hold=self.max_hold,
                               cash_target=self.cash_target, cash_low=self.cash_low, cash_high=self.cash_high,
                               band=self.band, ap_margin=self.ap_target - ACTIVE_SHARE_MIN,
                               soft_cap=self.tilt_cap_other, ap_max_iter=self.ap_max_iter)

    def cap(self, ticker: str) -> float:
        """tilt 後的單檔上限。"""
        return self.tilt_cap_tsmc if ticker == TSMC else self.tilt_cap_other


# --------------------------------------------------------------------------- Active Share 修正（三段式）

def _ap_clean(res: dict | None) -> bool:
    return res is None or not (res["fails"] or res["warnings"])


def ensure_active_share(weights: dict[str, float], top10: dict[str, dict[str, float]] | None,
                        p: PortfolioParams) -> tuple[dict[str, float], dict | None, str]:
    """把 Active Share 補到 20% + p.ap_margin（final 策略為 25%）。每一段都從原權重重新出發（不累積）：
       minimal   B 的最小調整法：只動 1–3 檔（單檔最多降到一半），減下的權重給第 11–28 名
       wider     放寬到 6 檔、單檔最多降到 30%
       full      全面壓低重疊最大者（fit_active_share）
    回傳 (權重, 檢查結果, 使用的段落)；三段都達不到 25% 時回傳其中最高者（呼叫端另行檢查官方 20%）。"""
    if not top10:
        return dict(weights), None, "none"
    res = check_target_weights(weights, top10, margin=p.ap_margin, expected_etfs=list(top10))
    if _ap_clean(res):
        return dict(weights), res, "none"
    tries = [("minimal", lambda: fit_active_share_minimal(weights, top10, p)),
             ("wider", lambda: fit_active_share_minimal(weights, top10, p, max_names=6, floor_ratio=0.3)),
             ("full", lambda: fit_active_share(weights, None, top10, p))]
    best = None
    for tier, fn in tries:
        w, r = fn()
        if _ap_clean(r):
            return w, r, tier
        if best is None or r["min"] > best[1]["min"]:
            best = (w, r, tier)
    return best


# --------------------------------------------------------------------------- 基準 A

def raw_base(fdf: pd.DataFrame, cap: pd.Series, p: FinalParams) -> tuple[dict[str, float], list[str]]:
    """(b) 權重取前 n_base 檔重新歸一（未做 Active Share 修正）。回傳 (權重, 合格名單)。"""
    pp = p.portfolio_params()
    liquid = list(fdf.index[(fdf["adv20"] >= p.min_adv) & cap.reindex(fdf.index).notna()])
    if len(liquid) < p.n_base:
        raise ValueError(f"合格股票只有 {len(liquid)} 檔，少於 {p.n_base}")
    b = capped_cap_weights(cap.reindex(liquid).dropna(), p.base_cap_tsmc, p.base_cap_other)
    top = sorted(b, key=lambda t: (-b[t], t))[:p.n_base]
    caps = {t: min(cap_of(t, pp), p.base_cap_tsmc if t == TSMC else p.base_cap_other) for t in top}
    return _waterfill({t: b[t] for t in top}, 1.0 - p.cash_target, caps), liquid


def base_plan(fdf: pd.DataFrame, cap: pd.Series, p: FinalParams,
              top10: dict[str, dict[str, float]] | None) -> tuple[dict[str, float], list[str], list[str]]:
    """A 的權重 ＋ Active Share 補到目標。回傳 (權重, 合格名單, notes)。"""
    w, liquid = raw_base(fdf, cap, p)
    w, res, tier = ensure_active_share(w, top10, p.portfolio_params())
    notes = [] if tier == "none" else [f"base_active_share_fix:{tier}"]
    return w, liquid, notes


# --------------------------------------------------------------------------- tilt

@dataclass
class TiltResult:
    weights: dict[str, float]
    requested: dict[str, float]
    applied: dict[str, float]               # 實際生效的 delta（相對 base，含被截斷／縮放／Active Share 修正的影響）
    notes: list[str] = field(default_factory=list)
    active_share: dict | None = None

    @property
    def cash(self) -> float:
        return 1.0 - sum(self.weights.values())

    @property
    def n_hold(self) -> int:
        return sum(1 for w in self.weights.values() if w > 1e-9)

    @property
    def active_share_total(self) -> float:
        """Σ|實際偏離| / 2。"""
        return sum(abs(d) for d in self.applied.values()) / 2


def validate_tilts(base: dict[str, float], tilts: dict[str, float], p: FinalParams,
                   eligible: set[str] | None = None) -> list[str]:
    """逐條檢查 tilt（不修改任何東西）。回傳錯誤訊息清單，空 = 合法。"""
    uni = load_universe()
    errs = []
    for t, d in tilts.items():
        if t not in uni:
            errs.append(f"{t} 不在 150 檔名單內")
            continue
        if not isinstance(d, (int, float)) or not math.isfinite(d):
            errs.append(f"{t} 的 delta 不是有限數字: {d!r}")
            continue
        if abs(d) > p.max_delta + 1e-12:
            errs.append(f"{t} 的 |delta|={abs(d):.4f} 超過單檔上限 {p.max_delta:.2%}")
        if d > 0 and eligible is not None and t not in eligible:
            errs.append(f"{t} 當日無價格或流動性不足，不可加碼")
        if d < 0 and base.get(t, 0.0) <= 1e-12:
            errs.append(f"{t} 基準未持有，不可減碼（不可放空）")
    total = sum(abs(d) for d in tilts.values() if isinstance(d, (int, float)) and math.isfinite(d)) / 2
    if total > p.max_active + 1e-12:
        errs.append(f"主動偏離 Σ|delta|/2={total:.4f} 超過上限 {p.max_active:.2%}")
    return errs


def apply_tilts(base: dict[str, float], tilts: dict[str, float], *, params: FinalParams | None = None,
                eligible: set[str] | None = None, top10: dict[str, dict[str, float]] | None = None,
                strict: bool = True) -> TiltResult:
    """把 tilt 套到 base 上並重新檢查。strict=True（T4 與 LLM 互動用）：tilt 不合法 → TiltError，錯誤逐條列出；
    strict=False（每日排程的保底）：不合法的 tilt 直接略過，並記在 notes。
    套用後依序處理：單檔上限（超過者截到上限）→ 持股檔數（> max_hold 時丟掉最小的新增檔）→ 現金
    （< 下限時等比例縮小加碼）→ Active Share（補到目標，必要時動到 tilt 本身）。"""
    p = params or FinalParams()
    pp = p.portfolio_params()
    notes: list[str] = []
    tilts = {t: float(d) for t, d in tilts.items() if d}
    errs = validate_tilts(base, tilts, p, eligible)
    if errs:
        if strict:
            raise TiltError(errs)
        bad = {e.split(" ")[0] for e in errs if not e.startswith("主動偏離")}
        notes += [f"tilt_dropped:{e}" for e in errs if not e.startswith("主動偏離")]
        tilts = {t: d for t, d in tilts.items() if t not in bad}
        total = sum(abs(d) for d in tilts.values()) / 2
        if total > p.max_active:
            k = p.max_active / total
            tilts = {t: d * k for t, d in tilts.items()}
            notes.append(f"tilt_scaled:{k:.3f}")
    requested = dict(tilts)

    new = {t: w for t, w in base.items() if w > 1e-12}
    for t, d in tilts.items():
        new[t] = max(0.0, new.get(t, 0.0) + d)

    # --- 單檔上限
    for t in list(new):
        if new[t] > p.cap(t) + 1e-12:
            notes.append(f"cap_clipped:{t}")
            new[t] = p.cap(t)
    # --- 持股檔數
    added = sorted((t for t in new if base.get(t, 0.0) <= 1e-12 and new[t] > 1e-12), key=lambda t: new[t])
    while sum(1 for w in new.values() if w > 1e-12) > p.max_hold and added:
        t = added.pop(0)
        notes.append(f"max_hold_dropped:{t}")
        new[t] = 0.0
    n = sum(1 for w in new.values() if w > 1e-12)
    if not p.min_hold <= n <= p.max_hold:
        raise TiltError([f"套用後持股 {n} 檔，不在 {p.min_hold}–{p.max_hold} 檔"])
    # --- 現金
    limit = 1.0 - p.cash_low
    excess = sum(new.values()) - limit
    if excess > 1e-12:
        inc = {t: new[t] - base.get(t, 0.0) for t in new if new[t] - base.get(t, 0.0) > 1e-12}
        tot_inc = sum(inc.values())
        if tot_inc <= excess:
            raise TiltError(["現金低於下限且無法靠縮小加碼補回"])
        k = 1.0 - excess / tot_inc
        for t, d in inc.items():
            new[t] = base.get(t, 0.0) + d * k
        notes.append(f"cash_scaled:{k:.3f}")
    if 1.0 - sum(new.values()) > p.cash_high + 1e-12:
        raise TiltError([f"現金 {1.0 - sum(new.values()):.2%} 高於上限 {p.cash_high:.0%}"])
    # --- Active Share
    new, ap, tier = ensure_active_share({t: w for t, w in new.items() if w > 1e-12}, top10, pp)
    if tier != "none":
        notes.append(f"active_share_fix:{tier}")
    for t in list(new):                                # 修正可能讓接收者超過 tilt 上限（只可能是 2330），再截一次
        if new[t] > p.cap(t) + 1e-12:
            new[t] = p.cap(t)
    new = {t: w for t, w in new.items() if w > 1e-12}
    if ap is not None and not ap["ok"]:
        msg = f"Active Share 最低 {ap['min']:.1%} 低於官方門檻 {ACTIVE_SHARE_MIN:.0%}"
        if strict:
            raise TiltError([msg])
        notes.append("active_share_below_official")
    applied = {t: new.get(t, 0.0) - base.get(t, 0.0) for t in set(new) | set(base)
               if abs(new.get(t, 0.0) - base.get(t, 0.0)) > 1e-9}
    return TiltResult(weights=new, requested=requested, applied=applied, notes=notes, active_share=ap)


# --------------------------------------------------------------------------- 策略物件（回測與每日流程共用）

@dataclass
class FinalStrategy:
    name: str = "final"
    params: FinalParams = field(default_factory=FinalParams)
    uses_tilts: bool = True
    _ctx: tuple | None = field(default=None, repr=False)

    @property
    def rebalance_every(self) -> int:
        return self.params.rebalance_every

    @property
    def min_adv(self) -> float:
        return self.params.min_adv

    # ---- base_weights(date)：需要先 bind 資料
    def bind(self, factors: dict[str, pd.DataFrame], caps: pd.DataFrame,
             top10: dict[str, dict[str, float]] | None) -> "FinalStrategy":
        self._ctx = (factors, caps, top10)
        return self

    def base_weights(self, date: str) -> dict[str, float]:
        """date = 決策日 D（只用 D 日以前資料）。回傳 A 的權重（已補 Active Share 到 25%）。"""
        if self._ctx is None:
            raise RuntimeError("請先 bind(factors, caps, top10)")
        from .factors import factors_on
        factors, caps, top10 = self._ctx
        return base_plan(factors_on(factors, date), caps.loc[date], self.params, top10)[0]

    # ---- 提前再平衡條件
    def force_rebalance(self, current: dict[str, float], top10: dict[str, dict[str, float]] | None) -> bool:
        p = self.params
        if any(w > (p.guard_tsmc if t == TSMC else p.guard_other) for t, w in current.items()):
            return True
        if top10 and current:
            ap = check_target_weights(current, top10, margin=0.0, expected_etfs=list(top10))
            return ap["min"] < ACTIVE_SHARE_MIN + p.ap_guard
        return False

    # ---- 一次決策
    def decide(self, fdf: pd.DataFrame, cap: pd.Series, current: dict[str, float],
               top10: dict[str, dict[str, float]] | None, tilts: dict[str, float] | None = None) -> PortfolioResult:
        p, pp = self.params, self.params.portfolio_params()
        base, liquid, notes = base_plan(fdf, cap, p, top10)
        target, ap = base, None
        if tilts:
            tr = apply_tilts(base, tilts, params=p, eligible=set(liquid), top10=top10, strict=False)
            target, ap, notes = tr.weights, tr.active_share, notes + tr.notes
        banded = apply_no_trade_band(target, current, pp)
        if banded != target:
            ap2 = check_target_weights(banded, top10, margin=pp.ap_margin, expected_etfs=list(top10)) if top10 else None
            if ap2 is None or _ap_clean(ap2):
                target, ap = banded, ap2
            else:
                notes = notes + ["band_dropped_for_active_share"]
        return PortfolioResult(weights={t: w for t, w in target.items() if w > 0}, selected=sorted(target),
                               cash=1.0 - sum(target.values()), active_share=ap, notes=notes)


# --------------------------------------------------------------------------- 模擬 Agent 雜訊

def random_tilt_fn(seed: int, n_names: int = 5, lo: float = 0.01, hi: float = 0.03, period: int = 5):
    """隨機 tilt 產生器（回測用，模擬 Agent 雜訊）：每 period 個交易日重抽一次（period=1 為每天都變的壓力測試）。
    每次抽 n_names 檔：一半機率加碼（從合格名單任選，含不在前 28 的股票）、一半機率減碼（從現有持股任選，
    持股不足時改加碼），幅度 U(lo, hi)。同一個 seed 與日期序列得到相同結果。"""
    import random
    cache: dict[int, dict] = {}
    day_idx: dict[str, int] = {}

    def fn(D: str, eligible: list[str], current: dict[str, float]) -> dict[str, float]:
        k = day_idx.setdefault(D, len(day_idx)) // period
        if k not in cache:
            rng = random.Random(f"{seed}:{k}")
            held = sorted(t for t, w in current.items() if w > 0.01)
            out: dict[str, float] = {}
            while len(out) < n_names and len(out) < len(eligible):
                if held and rng.random() < 0.5:
                    t, sign = rng.choice(held), -1.0
                else:
                    t, sign = rng.choice(sorted(eligible)), 1.0
                if t not in out:
                    out[t] = sign * rng.uniform(lo, hi)
            cache[k] = out
        return dict(cache[k])

    return fn
