"""台股交易日曆：TWSE OpenAPI 休市日 holidaySchedule。

回應：[{"Name": "國慶日", "Date": "1151010", "Weekday": "六", "Description": "依規定放假1日。"}, ...]（民國年月日）。
清單中也包含「開始交易／最後交易」之類仍有交易的日子，那些不算休市。
抓到的清單快取在 data/reference/holidays_<西元年>.json；抓不到時只排除週末並警告。
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from ..config import REFERENCE_DIR
from .http import Fetcher, decode

HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
TRADING_KEYWORDS = ("開始交易", "最後交易", "開始收盤", "交易日")


def roc_to_date(s: str) -> date:
    s = re.sub(r"\D", "", str(s))
    if len(s) == 7:
        return date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:]))
    if len(s) == 8:
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    raise ValueError(f"無法解析日期 {s!r}")


def parse_holidays(records: list[dict]) -> list[str]:
    """回傳休市日（YYYY-MM-DD，已排序）；名稱或說明含「開始交易／最後交易」者為交易日，排除。"""
    out = set()
    for r in records:
        text = f"{r.get('Name', '')}{r.get('Description', '')}"
        if any(k in text for k in TRADING_KEYWORDS) and "無交易" not in text:
            continue
        try:
            out.add(roc_to_date(r.get("Date", "")).isoformat())
        except ValueError:
            continue
    return sorted(out)


def cache_path(year: int, base: Path | None = None) -> Path:
    return (base or REFERENCE_DIR) / f"holidays_{year}.json"


def fetch_holidays(fetcher: Fetcher, base: Path | None = None) -> list[str]:
    days = parse_holidays(json.loads(decode(fetcher.get(HOLIDAY_URL, name="holidaySchedule.json"))))
    for y in sorted({d[:4] for d in days}):
        cache_path(int(y), base).write_text(json.dumps([d for d in days if d.startswith(y)], indent=1), encoding="utf-8")
    return days


def load_holidays(years, base: Path | None = None) -> tuple[set[str], list[int]]:
    """回傳 (休市日集合, 沒有快取的年份)。"""
    out, missing = set(), []
    for y in sorted(set(years)):
        p = cache_path(y, base)
        if p.exists():
            out |= set(json.loads(p.read_text(encoding="utf-8")))
        else:
            missing.append(y)
    return out, missing


def is_trading_day(d: date, holidays: set[str]) -> bool:
    return d.weekday() < 5 and d.isoformat() not in holidays


def next_trading_day(d: date, holidays: set[str]) -> date:
    n = d + timedelta(days=1)
    while not is_trading_day(n, holidays):
        n += timedelta(days=1)
    return n


def prev_trading_day(d: date, holidays: set[str], inclusive: bool = True) -> date:
    n = d if inclusive else d - timedelta(days=1)
    while not is_trading_day(n, holidays):
        n -= timedelta(days=1)
    return n
