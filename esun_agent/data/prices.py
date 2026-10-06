"""價格面板與還原股價。

data/market 是「未還原」價格。分割／面額變更（如 2327、6919、8932、6669）會造成單日跳空，
因子與回測報酬一律用還原價（adj_close）；模擬下單股數與成交仍用原始價格。

偵測：同一檔股票「相對上一個有資料日」的收盤價比 r，若 r < SPLIT_LOW 或 r > SPLIT_HIGH 視為分割/減資。
（一般單日最大跌幅：漲跌停 ±10% 加上除息參考價下調，實測最差約 −22%；分割至少 1/2，兩者可分。）
分割倍數 k 取自常見比例 SPLIT_RATIOS（r<1：每 1 股變 k 股；r>1：減資，每 k 股變 1 股），
還原因子取該比例而非 r 本身，這樣分割當天的真實漲跌幅（如漲停 +9.9%）得以保留。
現金股利造成的除息缺口不還原（官方規則：除息現金期末才一次加回，回測 NAV 亦不含股利）。
"""
from __future__ import annotations

import glob
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from ..config import ROOT

MARKET_DIR = ROOT / "data" / "market"
INSTITUTIONAL_DIR = ROOT / "data" / "institutional"

SPLIT_LOW = 0.6
SPLIT_HIGH = 1.67
# 常見分割/減資倍數；取讓「分割當天殘餘漲跌幅 |r·k − 1|」最小者（6919：1/0.1099≈9.1，但 10 倍才是自然比例且當天 +9.9% 漲停）
SPLIT_RATIOS = (2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 50, 100)


@dataclass(frozen=True)
class SplitEvent:
    date: str
    ticker: str
    ratio: float      # 股數倍數：>1 分割（1 股變 ratio 股）；<1 減資（持股變少）
    raw_ratio: float  # 實際收盤價比 close_t / close_prev


def _load_dir(path: Path, columns: list[str]) -> pd.DataFrame:
    files = sorted(glob.glob(str(path / "????-??-??.parquet")))
    if not files:
        raise FileNotFoundError(f"{path} 下沒有 parquet")
    df = pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)
    df["date"] = df["date"].astype(str)
    df["ticker"] = df["ticker"].astype(str)
    return df[columns]


def detect_splits(close: pd.DataFrame) -> list[SplitEvent]:
    """close：date×ticker 的未還原收盤價（缺資料為 NaN）。"""
    events = []
    for t in close.columns:
        s = close[t].dropna()
        r = s / s.shift(1)
        for d, v in r[(r < SPLIT_LOW) | (r > SPLIT_HIGH)].items():
            if v < 1:
                k = float(min(SPLIT_RATIOS, key=lambda k: abs(v * k - 1)))
            else:
                k = 1 / float(min(SPLIT_RATIOS, key=lambda k: abs(v / k - 1)))
            events.append(SplitEvent(str(d), str(t), float(k), float(v)))
    return sorted(events, key=lambda e: (e.date, e.ticker))


def adjustment_factors(close: pd.DataFrame, events: list[SplitEvent]) -> pd.DataFrame:
    """還原因子 F（date×ticker）：adj = raw × F，F 為「該日之後」所有事件的 1/ratio 連乘，最新一段 F=1。"""
    F = pd.DataFrame(1.0, index=close.index, columns=close.columns)
    for e in events:
        F.loc[F.index < e.date, e.ticker] /= e.ratio
    return F


@dataclass
class Panel:
    """date×ticker 寬表。raw 為原始價；adj_close 為還原收盤價。"""
    close: pd.DataFrame
    open: pd.DataFrame
    avg_price: pd.DataFrame
    volume: pd.DataFrame
    value: pd.DataFrame
    foreign_net: pd.DataFrame
    trust_net: pd.DataFrame
    adj_close: pd.DataFrame
    events: list[SplitEvent]

    @property
    def dates(self) -> list[str]:
        return list(self.close.index)

    def split_ratios_on(self, date: str) -> dict[str, float]:
        return {e.ticker: e.ratio for e in self.events if e.date == date}


def build_panel(market: pd.DataFrame, inst: pd.DataFrame | None = None,
                events: list[SplitEvent] | None = None) -> Panel:
    wide = {c: market.pivot(index="date", columns="ticker", values=c).sort_index()
            for c in ("open", "close", "avg_price", "volume", "value")}
    close = wide["close"]
    if inst is not None and len(inst):
        f = inst.pivot(index="date", columns="ticker", values="foreign_net").reindex_like(close)
        t = inst.pivot(index="date", columns="ticker", values="trust_net").reindex_like(close)
    else:
        f = t = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    events = detect_splits(close) if events is None else events
    adj = close * adjustment_factors(close, events)
    return Panel(close=close, open=wide["open"], avg_price=wide["avg_price"], volume=wide["volume"],
                 value=wide["value"], foreign_net=f.fillna(0.0), trust_net=t.fillna(0.0),
                 adj_close=adj, events=events)


def load_panel(market_dir: Path | None = None, inst_dir: Path | None = None) -> Panel:
    cols = ["date", "ticker", "open", "close", "avg_price", "volume", "value"]
    market = _load_dir(market_dir or MARKET_DIR, cols)
    try:
        inst = _load_dir(inst_dir or INSTITUTIONAL_DIR, ["date", "ticker", "foreign_net", "trust_net"])
    except FileNotFoundError:
        inst = None
    return build_panel(market, inst)
