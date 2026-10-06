"""合規基準（對照組）：不加衛星，只用 (b) 市值加權上限的權重取前 n_hold 檔重新歸一。

  - (b) 權重 = 合格名單（有 D 日價格且 20 日均成交值 ≥ min_adv）的市值加權、2330 ≤ 20%、其他 ≤ 8%。
  - 取 (b) 權重最大的前 n_hold（28）檔，重新歸一到 1 − cash_target，單檔再受「官方上限 − 緩衝」限制。
  - Active Share 修正：ap_mode="full" = fit_active_share（A：全面壓低重疊最大者）；
                       ap_mode="minimal" = fit_active_share_minimal（B：只動 1–3 檔，減下的權重分給第 11–28 名）。
  - 之後套無交易帶（2%）；每 5 個交易日再平衡。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..config import ACTIVE_SHARE_MIN, TSMC
from ..data.etf_holdings import check_target_weights
from .marketcap import capped_cap_weights
from .portfolio import PortfolioParams, PortfolioResult, _waterfill, cap_of, finalize_weights


@dataclass(frozen=True)
class ComplianceParams:
    n_hold: int = 28
    cash_target: float = 0.03
    band: float = 0.02
    rebalance_every: int = 5
    min_adv: float = 2e8
    ap_margin: float = 0.02
    ap_mode: str = "full"             # "full"（A）| "minimal"（B）
    cap_tsmc: float = 0.20            # 單檔上限沿用 (b) 的 20%／8%（歸一化後會變大，所以重新套上限）
    cap_other: float = 0.08
    guard_tsmc: float = 0.24          # 再平衡之間的漂移保護：前日收盤權重超過這些值、或 Active Share 低於
    guard_other: float = 0.095        # 20% + guard_ap，就在當天提前再平衡（官方上限 25%／10%、Active Share 20%）
    guard_ap: float = 0.005

    def portfolio_params(self) -> PortfolioParams:
        return PortfolioParams(n_hold=self.n_hold, min_hold=20, max_hold=30, cash_target=self.cash_target,
                               band=self.band, ap_margin=self.ap_margin, soft_cap=0.095)


@dataclass
class ComplianceBenchmark:
    name: str
    params: ComplianceParams = field(default_factory=ComplianceParams)

    @property
    def rebalance_every(self) -> int:
        return self.params.rebalance_every

    @property
    def min_adv(self) -> float:
        return self.params.min_adv

    def force_rebalance(self, current: dict[str, float], top10: dict[str, dict[str, float]] | None) -> bool:
        """漂移保護：D 日收盤的現權重已逼近官方上限或 Active Share 下限時，不等 5 日週期、當天就再平衡。"""
        p = self.params
        if any(w > (p.guard_tsmc if t == TSMC else p.guard_other) for t, w in current.items()):
            return True
        if top10 and current:
            ap = check_target_weights(current, top10, margin=0.0, expected_etfs=list(top10))
            return ap["min"] < ACTIVE_SHARE_MIN + p.guard_ap
        return False

    def decide(self, fdf: pd.DataFrame, cap: pd.Series, current: dict[str, float],
               top10: dict[str, dict[str, float]] | None) -> PortfolioResult:
        p, pp = self.params, self.params.portfolio_params()
        liquid = fdf.index[(fdf["adv20"] >= p.min_adv) & cap.reindex(fdf.index).notna()]
        b = capped_cap_weights(cap.reindex(liquid).dropna())
        top = sorted(b, key=lambda t: (-b[t], t))[:p.n_hold]
        caps = {t: min(cap_of(t, pp), p.cap_tsmc if t == TSMC else p.cap_other) for t in top}
        target = _waterfill({t: b[t] for t in top}, 1.0 - p.cash_target, caps)
        return finalize_weights(target, current, pp, top10, top, minimal_ap=(p.ap_mode == "minimal"))
