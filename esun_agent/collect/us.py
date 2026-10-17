"""美股收盤（SPX、NDX、SOX）與 TSM ADR：免金鑰來源，依代號各自的來源順序嘗試，全失敗才缺值。

  SPX、NDX  FRED（聖路易聯準銀行）SP500 ／ NASDAQ100：有 FRED_API_KEY 用正式 API，否則 fredgraph.csv（見 collect/fred.py）。
            FRED 回應慢：逾時 60 秒、失敗重試 2 次。source url 用數列頁（不含金鑰）。FRED 常比 SOX／TSM 晚一天，只記 info。
  SOX、TSM  1. Nasdaq API historical（SS 實測可用）：
               https://api.nasdaq.com/api/quote/{SYMBOL}/historical?assetclass={index|stocks}&fromdate={D-14}&todate={D}&limit=10
               SOX=index、TSM=stocks；必帶瀏覽器 User-Agent、Accept: application/json, text/plain, */*、
               Origin／Referer = nasdaq.com（少了 fromdate/todate 或 Origin/Referer 會回 status.rCode 400）。
               data.tradesTable.rows[{date: "MM/DD/YYYY", close: "$412.61" 或 "11,137.29"}]（由新到舊）。
            2. CNBC quote webservice（一次查 .SOX|TSM）：FormattedQuoteResult.FormattedQuote[]
               {symbol, last, change_pct "-3.83%", previous_day_closing, last_time "2026-10-16"}。
            3. Yahoo chart JSON（瀏覽器 User-Agent；429 退避重試 2 次）。Stooq 已移除（確認 404）。
漲跌 % = 最新兩筆收盤（CNBC 用 change_pct，缺時以 last／previous_day_closing 計算）。
每個來源的 content_as_of = 該筆收盤日期的美東 16:00 換成台北時間（夏令 04:00、冬令 05:00）。
source url 用 nasdaq.com／cnbc.com／finance.yahoo.com 的行情頁（不放 API 網址）；authority = vendor。
任何代號全部來源失敗 → 該代號缺值；全部代號失敗 → None（daily_run 照常產生 D-Plan，只是少了美股 observation；
清晨模式只用台指期夜盤判斷）。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import fred
from .http import BROWSER_HEADERS, Fetcher, archive, decode

FRED_URL = fred.CSV_URL
NASDAQ_URL = "https://api.nasdaq.com/api/quote/{s}/historical?assetclass={a}&fromdate={f}&todate={t}&limit=10"
CNBC_URL = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols={s}"
            "&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json")
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=10d&interval=1d"
NASDAQ_LOOKBACK = 14

FRED_TIMEOUT, FRED_RETRIES = fred.TIMEOUT, fred.RETRIES    # 逾時 60 秒；第一次失敗後再重試 2 次
NASDAQ_HEADERS = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
                  "Referer": "https://www.nasdaq.com/"}

# 代號 → [(來源, 該來源的代號)]，依序嘗試；Nasdaq 為 (代號, assetclass)
SOURCES = {
    "SOX": [("nasdaq", ("SOX", "index")), ("cnbc", ".SOX"), ("yahoo", "%5ESOX")],
    "NDX": [("fred", "NASDAQ100")],
    "SPX": [("fred", "SP500")],
    "TSM": [("nasdaq", ("TSM", "stocks")), ("cnbc", "TSM"), ("yahoo", "TSM")],
}
CNBC_SYMBOLS = [sym for chain in SOURCES.values() for k, sym in chain if k == "cnbc"]
SOURCE_NAMES = {"fred": "FRED", "nasdaq": "Nasdaq", "cnbc": "CNBC", "yahoo": "Yahoo Finance"}
# 對外的行情頁（source url；不放 API 網址）
PAGE_URLS = {
    "nasdaq": {"SOX": "https://www.nasdaq.com/market-activity/index/sox/historical",
               "TSM": "https://www.nasdaq.com/market-activity/stocks/tsm/historical"},
    "cnbc": {".SOX": "https://www.cnbc.com/quotes/.SOX", "TSM": "https://www.cnbc.com/quotes/TSM"},
    "yahoo": {"%5ESOX": "https://finance.yahoo.com/quote/%5ESOX/history/",
              "TSM": "https://finance.yahoo.com/quote/TSM/history/"},
}
NY, TPE = ZoneInfo("America/New_York"), ZoneInfo("Asia/Taipei")


parse_fred = fred.parse_csv


def parse_yahoo(payload: dict) -> list[tuple[str, float]]:
    res = payload["chart"]["result"][0]
    ts, closes = res["timestamp"], res["indicators"]["quote"][0]["close"]
    out = [(datetime.fromtimestamp(t, NY).date().isoformat(), float(c)) for t, c in zip(ts, closes) if c is not None]
    if not out:
        raise ValueError("Yahoo chart 沒有收盤資料")
    return out


def _num(x) -> float:
    return float(str(x).replace("$", "").replace(",", "").replace("%", "").strip())


def parse_nasdaq(payload: dict) -> list[tuple[str, float]]:
    """data.tradesTable.rows[{date: "10/16/2026", close: "$412.61" 或 "11,137.29"}]（由新到舊）→ 由舊到新。
    status.rCode != 200（例如缺 fromdate/todate 或 Origin/Referer 時的 400）→ ValueError。"""
    status = (payload or {}).get("status") or {}
    if status.get("rCode") not in (None, 200):
        msg = status.get("bCodeMessage") or status.get("developerMessage") or ""
        raise ValueError(f"Nasdaq API rCode {status.get('rCode')}：{json.dumps(msg, ensure_ascii=False)[:120]}")
    rows = (((payload or {}).get("data") or {}).get("tradesTable") or {}).get("rows") or []
    out = []
    for r in rows:
        try:
            m, d, y = (int(x) for x in str(r["date"]).split("/"))
            out.append((date(y, m, d).isoformat(), _num(r["close"])))
        except (KeyError, ValueError, TypeError):
            continue
    if not out:
        raise ValueError("Nasdaq API 沒有收盤資料：" + json.dumps(payload, ensure_ascii=False)[:120])
    return sorted(out)


def parse_cnbc(payload: dict) -> dict[str, dict]:
    """FormattedQuoteResult.FormattedQuote[] → {symbol: {"date", "close", "chg_pct"}}。
    chg_pct 用 change_pct（"-3.83%"）；缺或無法解析時用 last／previous_day_closing 計算。日期取 last_time 的前 10 字。"""
    quotes = (((payload or {}).get("FormattedQuoteResult") or {}).get("FormattedQuote")) or []
    out = {}
    for q in quotes:
        try:
            sym, close = q["symbol"], _num(q["last"])
            d = date.fromisoformat(str(q["last_time"])[:10]).isoformat()
        except (KeyError, ValueError, TypeError):
            continue
        try:
            chg = _num(q["change_pct"])
        except (KeyError, ValueError, TypeError):
            try:
                chg = (close / _num(q["previous_day_closing"]) - 1) * 100
            except (KeyError, ValueError, TypeError, ZeroDivisionError):
                continue
        out[sym] = {"date": d, "close": close, "chg_pct": chg}
    if not out:
        raise ValueError("CNBC 沒有報價：" + json.dumps(payload, ensure_ascii=False)[:120])
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


def fetch_series(fetcher: Fetcher, kind: str, sym, not_after: date | None = None) -> tuple[list[tuple[str, float]], str]:
    """回傳 (序列, source url（行情頁，不是 API 網址）)。失敗丟例外。CNBC 不走這裡（見 fetch_cnbc）。"""
    if kind == "fred":
        series, url, _ = fred.fetch(fetcher, sym, end=not_after)
        return series, url
    if kind == "nasdaq":
        s, a = sym
        D = not_after or date.today()
        url = NASDAQ_URL.format(s=s, a=a, f=(D - timedelta(days=NASDAQ_LOOKBACK)).isoformat(), t=D.isoformat())
        raw = fetcher.get(url, name=f"us_nasdaq_{s}.json", headers={**BROWSER_HEADERS, **NASDAQ_HEADERS}, retries=2)
        return parse_nasdaq(json.loads(decode(raw))), PAGE_URLS["nasdaq"][s]
    if kind == "yahoo":
        url = YAHOO_URL.format(s=sym)
        raw = fetcher.get(url, name=f"us_yahoo_{sym.replace('%5E', '')}.json", headers=BROWSER_HEADERS,
                          retries=3, backoff=5.0, retry_status=(429,))          # 429 → 退避重試 2 次
        return parse_yahoo(json.loads(decode(raw))), PAGE_URLS["yahoo"][sym]
    raise ValueError(kind)


def fetch_cnbc(fetcher: Fetcher) -> dict[str, dict]:
    """一次查詢全部 CNBC 代號（.SOX|TSM）。"""
    url = CNBC_URL.format(s="%7C".join(CNBC_SYMBOLS))
    raw = fetcher.get(url, name="us_cnbc_quotes.json", headers=BROWSER_HEADERS, retries=2)
    return parse_cnbc(json.loads(decode(raw)))


def collect_us(fetcher: Fetcher, not_after: date | None = None, do_archive: bool = False) -> tuple[dict | None, list[str]]:
    """回傳 (us_input 或 None, warnings)。us_input = {url, content_as_of, name, quotes, sources, info}。
    sources[] 各自帶 content_as_of（該代號收盤日）；info 是不算警告的提示（例如 FRED 的 NDX／SPX 晚一天）。"""
    quotes, warns, srcs, kinds = {}, [], [], {}
    cnbc: dict | Exception | None = None
    for name, chain in SOURCES.items():
        q = None
        errs = []
        for kind, sym in chain:
            try:
                if kind == "cnbc":
                    if cnbc is None:
                        try:
                            cnbc = fetch_cnbc(fetcher)
                        except Exception as e:  # noqa: BLE001 - 只抓一次；失敗記住
                            cnbc = e
                    if isinstance(cnbc, Exception):
                        raise cnbc
                    if sym not in cnbc:
                        raise ValueError(f"CNBC 回應沒有 {sym}")
                    c = cnbc[sym]
                    if not_after and c["date"] > not_after.isoformat():
                        raise ValueError(f"CNBC {sym} 日期 {c['date']} 晚於 {not_after}")
                    q, url = dict(c), PAGE_URLS["cnbc"][sym]
                else:
                    series, url = fetch_series(fetcher, kind, sym, not_after)
                    q = last_change(series, not_after)
                break
            except Exception as e:  # noqa: BLE001 - 換下一個來源
                errs.append(f"{SOURCE_NAMES[kind]}：{str(e).splitlines()[0][:80]}")
        if q is None:
            warns.append(f"{name} 全部來源失敗（{'；'.join(errs)}），缺值")
            continue
        quotes[name], kinds[name] = q, kind
        srcs.append({"url": url, "name": f"{name} 日線收盤（{SOURCE_NAMES[kind]}）",
                     "content_as_of": close_time_taipei(q["date"])})
    if not quotes:
        return None, warns + ["美股／ADR 全部抓取失敗，今日不使用美股資料"]
    if do_archive:
        for s in srcs:
            a = archive(s["url"], fetcher.session)
            if a:
                s["archive_url"] = a
    latest = max(q["date"] for q in quotes.values())
    info = []
    stale = [n for n, q in quotes.items() if q["date"] != latest]
    fred_lag = [n for n in stale if kinds[n] == "fred"
                and _prev_weekday(date.fromisoformat(latest)) == date.fromisoformat(quotes[n]["date"])]
    if fred_lag:
        info.append(f"FRED 的 {', '.join(fred_lag)} 收盤為 {quotes[fred_lag[0]]['date']}，比 {latest} 晚一天（FRED 慣常延遲）")
    other = [n for n in stale if n not in fred_lag]
    if other:
        warns.append(f"美股收盤日期不一致（{', '.join(other)} 不是 {latest}）")
    return {"url": srcs[0]["url"], "content_as_of": close_time_taipei(latest), "name": f"美股／ADR 收盤（美國 {latest}）",
            "quotes": {n: {"close": q["close"], "chg_pct": round(q["chg_pct"], 4)} for n, q in quotes.items()},
            "sources": srcs[:5], "info": info}, warns


def _prev_weekday(d: date) -> date:
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p
