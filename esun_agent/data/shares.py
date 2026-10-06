"""發行股數（市值 = 收盤價 × 發行股數 的來源）。

- 上櫃：TPEx 日行情（與 market.py 同一個依日期查詢端點）的「發行股數」欄，已用真實回應驗證。
- 上市：TWSE 日行情 API（STOCK_DAY_ALL / STOCK_DAY）沒有發行股數；替代來源是 TWSE OpenAPI
  `t187ap03_L`（上市公司基本資料）的「已發行普通股數或TDR原發行股數」。該端點格式尚未用真實回應驗證
  （雲端環境連不到），解析時欄位找不到會直接報錯。
結果存成 data/reference/shares_outstanding.csv（ticker, shares, as_of, source），為單一時點快照。
回測以「還原收盤價 × 快照股數」估歷史市值（忽略期間增資／減資，分割已由還原價處理）。
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pandas as pd

from ..config import REFERENCE_DIR
from ..universe import load_universe
from .market import MarketFetcher, TWSE_STOCK_DAY_URL, TPEX_DAY_URL, to_number  # noqa: F401

SHARES_PATH = REFERENCE_DIR / "shares_outstanding.csv"
TWSE_BASIC_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
COLUMNS = ["ticker", "shares", "as_of", "source"]


def parse_tpex_shares(payload: dict, universe: dict | None = None) -> dict[str, int]:
    """TPEx 日行情回應（tables[0]，欄位含「代號」「發行股數」）→ {ticker: 發行股數}，只留名單內上櫃股。"""
    uni = universe if universe is not None else load_universe()
    tables = payload.get("tables") or []
    if not tables or not tables[0].get("data"):
        return {}
    f = [str(x).strip() for x in tables[0]["fields"]]
    ic = next(i for i, c in enumerate(f) if c == "代號")
    is_ = next((i for i, c in enumerate(f) if "發行股數" in c), None)
    if is_ is None:
        raise KeyError(f"TPEx 回應沒有「發行股數」欄位，現有欄位: {f}")
    out = {}
    for r in tables[0]["data"]:
        t = str(r[ic]).strip()
        v = to_number(r[is_])
        if t in uni and uni[t]["market"] == "TPEX" and v == v and v > 0:
            out[t] = int(v)
    return out


def parse_twse_basic(records: list[dict], universe: dict | None = None) -> dict[str, int]:
    """TWSE OpenAPI t187ap03_L → {ticker: 已發行普通股數}，只留名單內上市股。"""
    uni = universe if universe is not None else load_universe()
    out = {}
    for r in records:
        t = str(r.get("公司代號", "")).strip()
        if t not in uni or uni[t]["market"] != "TWSE":
            continue
        key = next((k for k in r if "已發行普通股數" in k), None)
        if key is None:
            raise KeyError(f"t187ap03_L 沒有「已發行普通股數」欄位，現有欄位: {list(r)}")
        v = to_number(r[key])
        if v == v and v > 0:
            out[t] = int(v)
    return out


def fetch_shares(day: date, fetcher: MarketFetcher | None = None) -> pd.DataFrame:
    """抓上市（t187ap03_L）與上櫃（TPEx 當日行情）發行股數，回傳 COLUMNS 欄位。"""
    f = fetcher or MarketFetcher()
    twse = parse_twse_basic(f.get_json(TWSE_BASIC_URL))
    payload = f.get_json(TPEX_DAY_URL, {"date": day.strftime("%Y/%m/%d"), "type": "EW", "response": "json"})
    tpex = parse_tpex_shares(payload)
    rows = [{"ticker": t, "shares": s, "as_of": day.isoformat(), "source": "twse_t187ap03_L"}
            for t, s in twse.items()]
    rows += [{"ticker": t, "shares": s, "as_of": day.isoformat(), "source": "tpex_daily"} for t, s in tpex.items()]
    return pd.DataFrame(rows, columns=COLUMNS).sort_values("ticker").reset_index(drop=True)


def save_shares(df: pd.DataFrame, path: Path | None = None) -> Path:
    p = path or SHARES_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    df[COLUMNS].to_csv(p, index=False, quoting=csv.QUOTE_MINIMAL)
    return p


def load_shares(path: Path | None = None, min_coverage: float = 0.95) -> pd.Series | None:
    """讀取快照 → Series(ticker → 股數)。檔案不存在或涵蓋率 < min_coverage（相對 150 檔）回傳 None
    （呼叫端改用替代市值）。"""
    p = path or SHARES_PATH
    if not p.exists():
        return None
    s = pd.read_csv(p, dtype={"ticker": str}).set_index("ticker")["shares"].astype(float)
    uni = load_universe()
    if len(set(s.index) & set(uni)) < min_coverage * len(uni):
        return None
    return s
