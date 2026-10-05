"""150 檔可投資名單與主動型 ETF 列表（由官方 PDF 轉出，見 data/reference/）。"""
import csv
from functools import lru_cache

from .config import REFERENCE_DIR


@lru_cache(maxsize=1)
def load_universe() -> dict[str, dict]:
    """回傳 {ticker: {"name":..., "market": "TWSE"|"TPEX"}}，共 150 檔。"""
    with open(REFERENCE_DIR / "universe_150.csv", encoding="utf-8") as f:
        return {r["ticker"]: {"name": r["name"], "market": r["market"]} for r in csv.DictReader(f)}


@lru_cache(maxsize=1)
def load_active_etfs() -> dict[str, str]:
    """回傳 {etf_code: name}，以 2026/9/11 證交所列表為準。"""
    with open(REFERENCE_DIR / "active_etfs.csv", encoding="utf-8") as f:
        return {r["etf_code"]: r["name"] for r in csv.DictReader(f)}


def in_universe(ticker: str) -> bool:
    return ticker in load_universe()


def authority_for(ticker: str) -> str:
    """D-Plan sources.authority：上市填 twse、上櫃填 tpex，不可混用。"""
    return "twse" if load_universe()[ticker]["market"] == "TWSE" else "tpex"
