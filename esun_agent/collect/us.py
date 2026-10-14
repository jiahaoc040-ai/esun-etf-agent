"""美股收盤（SOX、NDX、SPX）與 TSM ADR：免金鑰來源，主來源 Stooq 日線 CSV，失敗時改用 Yahoo chart JSON。

Stooq：https://stooq.com/q/d/l/?s=^sox&i=d → Date,Open,High,Low,Close,Volume（由舊到新）。
Yahoo：https://query1.finance.yahoo.com/v8/finance/chart/%5ESOX?range=10d&interval=1d → chart.result[0]
       {timestamp[], indicators.quote[0].close[]}。
漲跌 % = 最後兩個收盤價計算。content_as_of = 美東 16:00 換成台北時間（夏令 04:00、冬令 05:00）。
任何代號失敗 → 該代號缺值；全部失敗 → None（daily_run 照常產生 D-Plan，只是少了美股 observation）。
authority = vendor（行情服務商）。
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .http import Fetcher, archive, decode

SYMBOLS = {"SOX": ("^sox", "%5ESOX"), "NDX": ("^ndx", "%5ENDX"), "SPX": ("^spx", "%5EGSPC"), "TSM": ("tsm.us", "TSM")}
STOOQ_URL = "https://stooq.com/q/d/l/?s={s}&i=d"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=10d&interval=1d"
NY, TPE = ZoneInfo("America/New_York"), ZoneInfo("Asia/Taipei")


def parse_stooq(text: str) -> list[tuple[str, float]]:
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    out = []
    for r in rows:
        try:
            out.append((r["Date"], float(r["Close"])))
        except (KeyError, ValueError, TypeError):
            continue
    if not out:
        raise ValueError("Stooq CSV 沒有收盤資料：" + text[:80])
    return out


def parse_yahoo(payload: dict) -> list[tuple[str, float]]:
    res = payload["chart"]["result"][0]
    ts, closes = res["timestamp"], res["indicators"]["quote"][0]["close"]
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        out.append((datetime.fromtimestamp(t, NY).date().isoformat(), float(c)))
    if not out:
        raise ValueError("Yahoo chart 沒有收盤資料")
    return out


def last_change(series: list[tuple[str, float]], not_after: date | None = None) -> dict:
    s = [x for x in series if not_after is None or x[0] <= not_after.isoformat()]
    if len(s) < 2:
        raise ValueError("收盤資料不足兩筆")
    (d0, c0), (d1, c1) = s[-2], s[-1]
    return {"date": d1, "close": c1, "chg_pct": (c1 / c0 - 1) * 100}


def close_time_taipei(us_date: str) -> str:
    dt = datetime.combine(date.fromisoformat(us_date), datetime.min.time()).replace(hour=16, tzinfo=NY)
    return dt.astimezone(TPE).replace(microsecond=0).isoformat()


def collect_us(fetcher: Fetcher, not_after: date | None = None, do_archive: bool = False) -> tuple[dict | None, list[str]]:
    """回傳 (us_input 或 None, warnings)。us_input = {url, content_as_of, name, quotes, sources}。"""
    quotes, warns, srcs = {}, [], []
    for name, (stooq, yahoo) in SYMBOLS.items():
        q = url = None
        errs = []
        for kind, u in (("stooq", STOOQ_URL.format(s=stooq)), ("yahoo", YAHOO_URL.format(s=yahoo))):
            try:
                raw = decode(fetcher.get(u, name=f"us_{name}_{kind}.{'csv' if kind == 'stooq' else 'json'}"))
                series = parse_stooq(raw) if kind == "stooq" else parse_yahoo(json.loads(raw))
                q, url = last_change(series, not_after), u
                break
            except Exception as e:  # noqa: BLE001
                errs.append(f"{kind}：{e}")
        if q is None:
            warns.append(f"{name} 兩個來源都失敗（{'；'.join(errs)}），缺值")
            continue
        quotes[name] = q
        srcs.append({"url": url, "name": f"{name} 日線收盤（{'Stooq' if 'stooq' in url else 'Yahoo Finance'}）"})
    if not quotes:
        return None, warns + ["美股／ADR 全部抓取失敗，今日不使用美股資料"]
    if do_archive:
        for s in srcs:
            a = archive(s["url"], fetcher.session)
            if a:
                s["archive_url"] = a
    latest = max(q["date"] for q in quotes.values())
    stale = [n for n, q in quotes.items() if q["date"] != latest]
    if stale:
        warns.append(f"美股收盤日期不一致（{', '.join(stale)} 不是 {latest}）")
    return {"url": srcs[0]["url"], "content_as_of": close_time_taipei(latest), "name": f"美股／ADR 收盤（美國 {latest}）",
            "quotes": {n: {"close": q["close"], "chg_pct": round(q["chg_pct"], 4)} for n, q in quotes.items()},
            "sources": srcs[:5]}, warns
