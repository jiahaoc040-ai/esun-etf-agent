"""核心＋衛星策略。

  核心（core_share，預設 65%）：市值前 core_k 大，市值加權並套上限（2330 ≤ 20%、其他 ≤ 8%，皆為核心內占比）。
  衛星（1 − core_share，35%）：從「核心以外」的合格股票中，依綜合分數取前 sat_n 檔（≤ 10），
        分數 = Σ 權重 × z(因子)：投信 10 日淨買超（成交值標準化）、20 日動能（略過最近 5 日）、
        20 日波動度（懲罰，權重為負）；權重 ∝ clip(1 + tilt × 分數, 0.5, 2.0)，單檔受官方上限−緩衝限制。
        衛星只選核心以外的名單，所以持股數 = core_k + sat_n（預設 ≤ 28，必在 20–30 內）。
  現金：cash_target（預設 3%）；核心與衛星按 (1 − cash_target) 切分。
  之後：Active Share 修正（check_target_weights）→ 無交易帶（預設 2%）。每 rebalance_every 個交易日才再平衡。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..data.prices import Panel
from .factors import SAT_FACTOR_NAMES, eligible, score
from .marketcap import CORE_CAP_OTHER, CORE_CAP_TSMC, capped_cap_weights
from .portfolio import (PortfolioParams, PortfolioResult, _waterfill, cap_of, finalize_weights,
                        select_holdings)

DEFAULT_SAT_WEIGHTS = {"inst10": 1.0, "mom20s5": 1.0, "vol20": -1.0}


@dataclass(frozen=True)
class CoreSatelliteParams:
    core_k: int = 18
    core_share: float = 0.65
    sat_n: int = 10
    sat_weights: tuple = tuple(DEFAULT_SAT_WEIGHTS.items())   # frozen dataclass 需要 hashable
    tilt: float = 0.25
    keep_rank_buffer: int = 3          # 衛星現有持股排名 ≤ sat_n + 此值就保留
    cash_target: float = 0.03
    band: float = 0.02
    rebalance_every: int = 5
    min_adv: float = 2e8
    ap_margin: float = 0.02
    core_cap_tsmc: float = CORE_CAP_TSMC
    core_cap_other: float = CORE_CAP_OTHER

    def portfolio_params(self) -> PortfolioParams:
        return PortfolioParams(n_hold=self.core_k + self.sat_n, min_hold=20, max_hold=30,
                               cash_target=self.cash_target, band=self.band, ap_margin=self.ap_margin,
                               soft_cap=0.095, tilt=self.tilt)


@dataclass
class CoreSatellite:
    name: str
    params: CoreSatelliteParams = field(default_factory=CoreSatelliteParams)

    @property
    def rebalance_every(self) -> int:
        return self.params.rebalance_every

    @property
    def min_adv(self) -> float:
        return self.params.min_adv

    def decide(self, fdf: pd.DataFrame, cap: pd.Series, current: dict[str, float],
               top10: dict[str, dict[str, float]] | None) -> PortfolioResult:
        """fdf：D 日因子表；cap：D 日市值；current：現持股占 NAV 權重。回傳目標權重。"""
        p, pp = self.params, self.params.portfolio_params()
        # --- 核心：市值前 core_k（需有 D 日價格且通過流動性門檻）
        liquid = fdf.index[(fdf["adv20"] >= p.min_adv) & cap.reindex(fdf.index).notna()]
        capL = cap.reindex(liquid).dropna().sort_values(ascending=False)
        if len(capL) < p.core_k:
            raise ValueError(f"可選核心股票只有 {len(capL)} 檔")
        core = list(capL.index[:p.core_k])
        within = capped_cap_weights(capL.loc[core], p.core_cap_tsmc, p.core_cap_other)
        invested = 1.0 - p.cash_target
        target = {t: w * p.core_share * invested for t, w in within.items()}
        # --- 衛星：核心以外、因子齊全且流動性合格者
        uni = eligible(fdf, p.min_adv, SAT_FACTOR_NAMES).difference(core)
        sc = score(fdf, dict(p.sat_weights), uni)
        if len(sc) < p.sat_n:
            raise ValueError(f"可選衛星股票只有 {len(sc)} 檔")
        sat_pp = PortfolioParams(n_hold=p.sat_n, min_hold=p.sat_n, max_hold=p.sat_n,
                                 keep_rank_buffer=p.keep_rank_buffer)
        sat = select_holdings(sc, {t for t in current if t not in core}, sat_pp)
        raw = {t: min(2.0, max(0.5, 1.0 + p.tilt * float(sc[t]))) for t in sat}
        sat_w = _waterfill(raw, (1.0 - p.core_share) * invested, {t: cap_of(t, pp) for t in sat})
        target.update(sat_w)
        return finalize_weights(target, current, pp, top10, core + sat)
