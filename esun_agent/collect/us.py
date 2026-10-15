"""美股收盤（SPX、NDX、SOX）與 TSM ADR：免金鑰來源，依代號各自的來源順序嘗試，全失敗才缺值。

  SPX、NDX  FRED（聖路易聯準銀行）CSV：https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500 ／ NASDAQ100
            → 欄位 observation_date（舊版 DATE）,<series>；休市日值為 "."（略過）。FRED 回應慢：逾時 60 秒、失敗重試 2 次。
  SOX、TSM  Yahoo chart JSON（瀏覽器 User-Agent；429 退避重試 2 次）→ Nasdaq API historical
            （瀏覽器 User-Agent、Accept: application/json；assetclass 參數 SOX=index、TSM=stocks）。
  所有代號最後才試 Stooq 日線 CSV。
漲跌 % = 最後兩個收盤價計算。content_as_of = 美東 16:00 換成台北時間（夏令 04:00、冬令 05:00）。
任何代號全部來源失敗 → 該代號缺值；全部代號失敗 → None（daily_run 照常產生 D-Plan，只是少了美股 observation；
清晨模式只用台指期夜盤判斷）。authority = vendor（行情／數據服務商）。
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .http import BROWSER_HEADERS, Fetcher, archive, decode

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={s}"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=10d&interval=1d"
NASDAQ_URL = "https://api.nasdaq.com/api/quote/{s}/historical?assetclass={a}&limit=10"
STOOQ_URL = "https://stooq.com/q/d/l/?s={s}&i=d"

FRED_TIMEOUT, FRED_RETRIES = 60, 3                   # 逾時 60 秒；第一次失敗後再重試 2 次
NASDAQ_HEADERS = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
                  "Referer": "https://www.nasdaq.com/"}

# 代號 → [(來源, 該來源的代號)]，依序嘗試；Nasdaq 為 (代號, assetclass)
SOURCES = {
    "SOX": [("yahoo", "%5ESOX"), ("nasdaq", ("SOX", "index")), ("stooq", "^sox")],
    "NDX": [("fred", "NASDAQ100"), ("stooq", "^ndx")],
    "SPX": [("fred", "SP500"), ("stooq", "^spx")],
    "TSM": [("yahoo", "TSM"), ("nasdaq", ("TSM", "stocks")), ("stooq", "tsm.us")],
}
SOURCE_NAMES = {"fred": "FRED", "yahoo": "Yahoo Finance", "nasdaq": "Nasdaq", "stooq": "Stooq"}
NY, TPE = ZoneInfo("America/New_York"), ZoneInfo("Asia/Taipei")


def parse_fred(text: str) -> list[tuple[str, float]]:
    rows = list(csv.reader(io.StringIO(text.strip().lstrip("﻿"))))
    if not rows or len(rows[0]) < 2 or "date" not in rows[0][0].lower():
        raise ValueError("FRED CSV 格式不符：" + text[:80])
    out = []
    for r in rows[1:]:
        if len(r) < 2 or r[1].strip() in ("", "."):
            continue
        try:
            out.append((r[0].strip(), float(r[1])))
        except ValueError:
            continue
    if not out:
        raise ValueError("FRED CSV 沒有數值")
    return out


def parse_yahoo(payload: dict) -> list[tuple[str, float]]:
    res = payload["chart"]["result"][0]
    ts, closes = res["timestamp"], res["indicators"]["quote"][0]["close"]
    out = [(datetime.fromtimestamp(t, NY).date().isoformat(), float(c)) for t, c in zip(ts, closes) if c is not None]
    if not out:
        raise ValueError("Yahoo chart 沒有收盤資料")
    return out


def parse_nasdaq(payload: dict) -> list[tuple[str, float]]:
    """data.tradesTable.rows[{date: "10/26/2026", close: "$306.00"}]（由新到舊）→ 由舊到新。"""
    rows = (((payload or {}).get("data") or {}).get("tradesTable") or {}).get("rows") or []
    out = []
    for r in rows:
        try:
            m, d, y = (int(x) for x in str(r["date"]).split("/"))
            out.append((date(y, m, d).isoformat(), float(str(r["close"]).replace("$", "").replace(",", ""))))
        except (KeyError, ValueError, TypeError):
            continue
    if not out:
        raise ValueError("Nasdaq API 沒有收盤資料：" + json.dumps(payload, ensure_ascii=False)[:120])
    return sorted(out)


def parse_stooq(text: str) -> list[tuple[str, float]]:
    out = []
    for r in csv.DictReader(io.StringIO(text.strip())):
        try:
            out.append((r["Date"], float(r["Close"])))
        except (KeyError, ValueError, TypeError):
            continue
    if not out:
        raise ValueError("Stooq CSV 沒有收盤資料：" + text[:80])
    return out


def last_change(series: list[tuple[str, float]], not_after: date | None = None) -> dict:
    s = [x for x in series if not_after is None or x[0] <= not_after.isoformat()]
    if len(s) < 2:
        raise ValueError("收盤資料不足兩筆")
    (_, c0), (d1, c1) = s[-2], s[-1]
    return {"date": d1, "close": c1, "chg_pct": (c1 / c0 - 1) * 100}


def close_time_taipei(us_date: str) -> str:
    dt = datetime.combine(date.fromisoformat(us_date), datetime.min.time()).replace(hour=16, tzinfo=NY)
    return dt.astimezone(TPE).replace(microsecond=0).isoformat()


def fetch_series(fetcher: Fetcher, kind: str, sym) -> tuple[list[tuple[str, float]], str]:
    """回傳 (序列, 實際網址)。失敗丟例外。"""
    if kind == "fred":
        url = FRED_URL.format(s=sym)
        raw = fetcher.get(url, name=f"us_fred_{sym}.csv", headers=BROWSER_HEADERS, timeout=FRED_TIMEOUT,
                          retries=FRED_RETRIES)
        return parse_fred(decode(raw)), url
    if kind == "yahoo":
        url = YAHOO_URL.format(s=sym)
        raw = fetcher.get(url, name=f"us_yahoo_{sym.replace('%5E', '')}.json", headers=BROWSER_HEADERS,
                          retries=3, backoff=5.0, retry_status=(429,))          # 429 → 退避重試 2 次
        return parse_yahoo(json.loads(decode(raw))), url
    if kind == "nasdaq":
        s, a = sym
        url = NASDAQ_URL.format(s=s, a=a)
        raw = fetcher.get(url, name=f"us_nasdaq_{s}.json", headers={**BROWSER_HEADERS, **NASDAQ_HEADERS}, retries=2)
        return parse_nasdaq(json.loads(decode(raw))), url
    if kind == "stooq":
        url = STOOQ_URL.format(s=sym)
        return parse_stooq(decode(fetcher.get(url, name=f"us_stooq_{sym.replace('^', '')}.csv"))), url
    raise ValueError(kind)


def collect_us(fetcher: Fetcher, not_after: date | None = None, do_archive: bool = False) -> tuple[dict | None, list[str]]:
    """回傳 (us_input 或 None, warnings)。us_input = {url, content_as_of, name, quotes, sources}。"""
    quotes, warns, srcs = {}, [], []
    for name, chain in SOURCES.items():
        q = None
        errs = []
        for kind, sym in chain:
            try:
                series, url = fetch_series(fetcher, kind, sym)
                q = last_change(series, not_after)
                break
            except Exception as e:  # noqa: BLE001 - 換下一個來源
                errs.append(f"{SOURCE_NAMES[kind]}：{str(e).splitlines()[0][:80]}")
        if q is None:
            warns.append(f"{name} 全部來源失敗（{'；'.join(errs)}），缺值")
            continue
        quotes[name] = q
        srcs.append({"url": url, "name": f"{name} 日線收盤（{SOURCE_NAMES[kind]}）"})
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
