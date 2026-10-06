"""150 檔因子（全部只用「當日及以前」的資料；報酬類用還原價，量能類用原始量額）。

  mom5/mom20/mom60   還原收盤價 n 日報酬
  vol20              近 20 日日報酬標準差（還原價）
  foreign20/trust20  近 20 日外資／投信買賣超股數 ÷ 同期成交股數（法人淨買比，無單位）
  valchg             log(近 5 日均成交值 ÷ 近 20 日均成交值)
  inst10             近 10 日投信淨買超金額（Σ 淨買股數×均價）÷ 近 10 日成交值（成交值標準化）
  mom20s5            20 日動能並略過最近 5 日：adj[t−5] / adj[t−25] − 1
  adv20              近 20 日均成交值（不是因子，供流動性門檻用）

compute_factors 回傳 {因子名: date×ticker DataFrame}；factors_on(date) 取單日 ticker×因子表。
score() 把因子做橫斷面 z-score（±3 截尾）後加權。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.prices import Panel

FACTOR_NAMES = ["mom5", "mom20", "mom60", "vol20", "foreign20", "trust20", "valchg"]
SAT_FACTOR_NAMES = ["inst10", "mom20s5", "vol20"]          # 核心＋衛星策略的衛星分數用
MIN_HISTORY = 61  # mom60 需要 61 個收盤價


def compute_factors(panel: Panel, ffill_limit: int = 10) -> dict[str, pd.DataFrame]:
    px = panel.adj_close.ffill(limit=ffill_limit)  # 停牌短暫缺資料時沿用前價，避免因子斷掉
    ret = px.pct_change(fill_method=None)
    vol = panel.volume.fillna(0.0)
    val = panel.value.fillna(0.0)
    f: dict[str, pd.DataFrame] = {}
    for n in (5, 20, 60):
        f[f"mom{n}"] = px / px.shift(n) - 1
    f["vol20"] = ret.rolling(20, min_periods=20).std()
    vsum = vol.rolling(20, min_periods=10).sum().replace(0, np.nan)
    f["foreign20"] = panel.foreign_net.rolling(20, min_periods=10).sum() / vsum
    f["trust20"] = panel.trust_net.rolling(20, min_periods=10).sum() / vsum
    v5, v20 = val.rolling(5, min_periods=5).mean(), val.rolling(20, min_periods=20).mean()
    f["valchg"] = np.log(v5.replace(0, np.nan) / v20.replace(0, np.nan))
    val10 = val.rolling(10, min_periods=5).sum().replace(0, np.nan)
    f["inst10"] = (panel.trust_net * panel.avg_price.fillna(0.0)).rolling(10, min_periods=5).sum() / val10
    f["mom20s5"] = px.shift(5) / px.shift(25) - 1
    f["adv20"] = v20
    # 沒有當日收盤價（未上市、當日無成交）的股票不產生任何因子
    mask = panel.close.notna()
    return {k: v.where(mask) for k, v in f.items()}


def factors_on(factors: dict[str, pd.DataFrame], date: str) -> pd.DataFrame:
    return pd.DataFrame({k: v.loc[date] for k, v in factors.items()})


def eligible(fdf: pd.DataFrame, min_adv: float = 0.0, names: list[str] | None = None) -> pd.Index:
    """指定因子（預設 FACTOR_NAMES）齊全（≥61 天歷史、當日有收盤價）且 20 日均成交值 ≥ min_adv 的股票。"""
    ok = fdf[names or FACTOR_NAMES].notna().all(axis=1) & (fdf["adv20"] >= min_adv)
    return fdf.index[ok]


def zscore(s: pd.Series, clip: float = 3.0) -> pd.Series:
    sd = s.std(ddof=0)
    if not sd or np.isnan(sd):
        return s * 0.0
    return ((s - s.mean()) / sd).clip(-clip, clip)


def score(fdf: pd.DataFrame, weights: dict[str, float], universe: pd.Index) -> pd.Series:
    """Σ w_f × z_f，僅在 universe（合格者）內做橫斷面標準化。權重為負 = 偏好該因子低者（如 vol20）。"""
    sub = fdf.loc[universe]
    out = pd.Series(0.0, index=sub.index)
    for name, w in weights.items():
        if w:
            out += w * zscore(sub[name])
    return out.sort_values(ascending=False)
