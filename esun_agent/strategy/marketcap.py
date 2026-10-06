"""市值與市值加權（含上限）基準／核心權重。

市值 = 還原收盤價 × 發行股數快照（data/reference/shares_outstanding.csv，見 data/shares.py）。
快照不存在或涵蓋不足時改用「近 60 日均成交值」代理（source="adv60_proxy"）：它是時點正確、
但會偏重高週轉的熱門股（成交值 = 市值 × 週轉率），只是 TWSE 發行股數取得前的暫代。
"""
from __future__ import annotations

import pandas as pd

from ..config import TSMC
from ..data.prices import Panel
from ..data.shares import load_shares
from .portfolio import _waterfill

CORE_CAP_TSMC = 0.20
CORE_CAP_OTHER = 0.08


def market_cap(panel: Panel, shares: pd.Series | None = None, proxy_window: int = 60) -> tuple[pd.DataFrame, str]:
    """回傳 (date×ticker 市值, 來源說明)。沒有當日收盤價者為 NaN。"""
    mask = panel.close.notna()
    if shares is None:
        shares = load_shares()
    if shares is not None:
        cap = panel.adj_close.ffill(limit=10) * shares.reindex(panel.close.columns)
        return cap.where(mask), "issued_shares"
    adv = panel.value.fillna(0.0).rolling(proxy_window, min_periods=20).mean()
    return adv.where(mask), "adv60_proxy"


def capped_cap_weights(cap: pd.Series, cap_tsmc: float = CORE_CAP_TSMC, cap_other: float = CORE_CAP_OTHER,
                       budget: float = 1.0) -> dict[str, float]:
    """市值加權並套上限（2330 ≤ cap_tsmc，其他 ≤ cap_other），超過上限者釘在上限、餘額按市值重新分配。
    cap 為 Series（ticker → 市值，已去除 NaN）。上限總和不足 budget 時全部釘在上限（權重和 < budget）。"""
    cap = cap.dropna()
    cap = cap[cap > 0]
    caps = {t: (cap_tsmc if t == TSMC else cap_other) for t in cap.index}
    return _waterfill({t: float(v) for t, v in cap.items()}, budget, caps)
