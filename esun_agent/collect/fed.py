"""聯準會新聞稿 RSS：https://www.federalreserve.gov/feeds/press_all.xml

只取 FOMC statement 與利率決議（標題含「FOMC statement」，或含 federal funds rate／interest rate／policy rate 且
屬貨幣政策類），發布時間在 now 往前 36 小時內者 → macro observation（authority other，url＝新聞稿原文頁，
content_as_of＝pubDate 換成台北時間）。RSS 只有標題與連結，observation 的 values 只放則數。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from .http import Fetcher

FED_RSS_URL = "https://www.federalreserve.gov/feeds/press_all.xml"
TPE = ZoneInfo("Asia/Taipei")
LOOKBACK = timedelta(hours=36)
STATEMENT_RE = re.compile(r"FOMC statement", re.I)
RATE_RE = re.compile(r"federal funds rate|interest rate|policy rate|target range", re.I)


def parse_rss(xml_text: str | bytes) -> list[dict]:
    """→ [{"title", "url", "published" (datetime, aware), "category"}]，順序同 RSS。"""
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        if not title or not link or not pub:
            continue
        try:
            dt = parsedate_to_datetime(pub.strip())
        except (TypeError, ValueError):
            continue
        out.append({"title": title, "url": link, "published": dt, "category": (it.findtext("category") or "").strip()})
    return out


def is_policy_release(item: dict) -> bool:
    if STATEMENT_RE.search(item["title"]):
        return True
    return bool(RATE_RE.search(item["title"])) and "monetary" in item.get("category", "").lower()


def collect_fed(fetcher: Fetcher, now: datetime) -> tuple[list[dict], list[str]]:
    """回傳 ([{"title", "url", "content_as_of"}], warnings)：now 往前 36 小時內、且不晚於 now 的 FOMC 聲明／利率決議。"""
    try:
        items = parse_rss(fetcher.get(FED_RSS_URL, name="fed_press_all.xml", retries=2))
    except Exception as e:  # noqa: BLE001
        return [], [f"聯準會新聞稿 RSS 抓取失敗（{type(e).__name__}: {str(e).splitlines()[0][:80]}）"]
    out = []
    for it in items:
        if is_policy_release(it) and now - LOOKBACK <= it["published"] <= now:
            out.append({"title": it["title"], "url": it["url"],
                        "content_as_of": it["published"].astimezone(TPE).replace(microsecond=0).isoformat()})
    return out, []
