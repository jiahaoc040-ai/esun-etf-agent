"""MOPS 當日重大訊息（限 150 檔）：TWSE OpenAPI t187ap04_L（上市）、TPEx OpenAPI mopsfe_t187ap04_O（上櫃）。

欄位（OpenAPI 為中文鍵，部分鍵尾端有空白）：出表日期、發言日期（民國 1151026）、發言時間（173005 或 17:30:05）、
公司代號、公司名稱、主旨、符合條款、事實發生日、說明。
只保留決策日 D 當天、發言時間不晚於 D 19:30（宣告檔：引用的證據 content_as_of 不得晚於決策日 19:30）的訊息；
較晚的訊息列入 late（記警告，隔天的決策才可引用）。
"""
from __future__ import annotations

import json
import re
from datetime import date

from ..universe import load_universe
from .calendar import roc_to_date
from .http import Fetcher, decode

TWSE_NEWS_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
TPEX_NEWS_URL = "https://www.tpex.org.tw/openapi/v1/mopsfe_t187ap04_O"
MOPS_NEWS_PAGE = "https://mops.twse.com.tw/mops/#/web/t05sr01_1"
CUTOFF = "19:30:00"


def _get(r: dict, *keys: str) -> str:
    norm = {re.sub(r"\s+", "", str(k)): v for k, v in r.items()}
    for k in keys:
        for nk, v in norm.items():
            if k in nk:
                return str(v or "").strip()
    return ""


def _time(s: str) -> str:
    d = re.sub(r"\D", "", s)
    d = (d + "000000")[:6] if len(d) < 6 else d[:6]
    return f"{d[:2]}:{d[2:4]}:{d[4:6]}"


def parse_news(records: list[dict], universe: dict | None = None) -> list[dict]:
    """→ [{ticker, name, date, time, subject, clause, detail}]（只留 150 檔，依時間排序）。"""
    uni = universe if universe is not None else load_universe()
    out = []
    for r in records:
        t = _get(r, "公司代號")
        if t not in uni:
            continue
        try:
            d = roc_to_date(_get(r, "發言日期"))
        except ValueError:
            continue
        subject = re.sub(r"\s+", " ", _get(r, "主旨"))
        if not subject:
            continue
        out.append({"ticker": t, "name": _get(r, "公司名稱") or uni[t]["name"], "date": d.isoformat(),
                    "time": _time(_get(r, "發言時間")), "subject": subject, "clause": _get(r, "符合條款"),
                    "detail": re.sub(r"\s+", " ", _get(r, "說明"))})
    return sorted(out, key=lambda x: (x["date"], x["time"], x["ticker"]))


def news_event(n: dict, url: str) -> dict:
    st = f"{n['ticker']} {n['name']} 重大訊息（{n['date']} {n['time'][:5]}）：{n['subject']}"
    hhmm = int(n["time"][:2] + n["time"][3:5])
    return {"basis": "material_news", "ticker": n["ticker"], "authority": "mops", "url": url,
            "name": f"MOPS 重大訊息 {n['ticker']} {n['date']}", "published_at": f"{n['date']}T{n['time']}+08:00",
            "content_as_of": f"{n['date']}T{n['time']}+08:00",
            "statement": st if len(st) <= 500 else st[:499] + "…", "values": {"announce_hhmm": hhmm}}


def select_day(items: list[dict], D: date, cutoff: str = CUTOFF) -> tuple[list[dict], list[dict]]:
    """回傳 (D 日且不晚於 cutoff 者, D 日但晚於 cutoff 者)。"""
    day = [n for n in items if n["date"] == D.isoformat()]
    return [n for n in day if n["time"] <= cutoff], [n for n in day if n["time"] > cutoff]


def collect_news(fetcher: Fetcher, D: date) -> tuple[list[dict], list[str]]:
    """回傳 (events, warnings)。任一市場失敗只記警告。"""
    events, warns = [], []
    for label, url in (("上市", TWSE_NEWS_URL), ("上櫃", TPEX_NEWS_URL)):
        try:
            items = parse_news(json.loads(decode(fetcher.get(url, name=f"news_{label}_{D:%Y%m%d}.json"))))
        except Exception as e:  # noqa: BLE001
            warns.append(f"重大訊息（{label}）抓取失敗：{e}")
            continue
        ok, late = select_day(items, D)
        events += [news_event(n, url) for n in ok]
        if late:
            warns.append(f"重大訊息（{label}）有 {len(late)} 則發言時間晚於 {D} {CUTOFF[:5]}，今日不可引用："
                         + "、".join(f"{n['ticker']}" for n in late[:10]))
    return events, warns
