"""收集層 parser 與降級行為（fixture，不連網）。"""
import json
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import pytest

from esun_agent.collect import calendar as cal
from esun_agent.collect import news, revenue, taifex, us
from esun_agent.collect.htmlrows import table_rows
from esun_agent.collect.http import Fetcher, decode, raw_name

FX = Path(__file__).parent / "fixtures" / "collect"
TZ = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """退避重試不要真的睡（Fetcher 與各模組共用 time.sleep）。"""
    import esun_agent.collect.http as h
    monkeypatch.setattr(h.time, "sleep", lambda s: None)


def fx(name) -> bytes:
    return (FX / name).read_bytes()


class FakeResp:
    def __init__(self, content=b"", status=200, headers=None):  # noqa: D401
        self.content, self.status_code, self.headers = content, status, headers or {}
        self.ok = status < 400
        self.text = content.decode("utf-8", errors="replace")

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """routes：[(url 片段, bytes 或 Exception)]，依序比對。"""
    def __init__(self, routes):
        self.routes, self.headers, self.calls, self.kw = routes, {}, [], []

    def _hit(self, url, **kw):
        self.calls.append(url)
        self.kw.append(kw)
        for i, (frag, r) in enumerate(self.routes):
            if frag in url:
                if isinstance(r, list):                          # 依序回應（例如先 429 再成功）
                    r = r.pop(0) if len(r) > 1 else r[0]
                if isinstance(r, int):
                    return FakeResp(b"", r)
                if isinstance(r, Exception):
                    import requests
                    raise r if isinstance(r, requests.RequestException) else requests.ConnectionError(str(r))
                return FakeResp(r)
        return FakeResp(b"", 404)

    def get(self, url, **kw):
        return self._hit(url, **kw)

    def post(self, url, **kw):
        return self._hit(url, **kw)


def fetcher(routes, tmp_path=None):
    return Fetcher(raw_dir=tmp_path, session=FakeSession(routes), retries=1)


# ------------------------------------------------------------------ 共用
def test_decode_big5_and_nested_rows():
    html = decode(fx("mops_t21sc03_sii_202609.html"))
    assert "台積電" in html
    rows = table_rows(html)
    assert ["2330", "台積電"] == rows[[r[0] for r in rows].index("2330")][:2]
    assert any(r[0] == "產業別：半導體業" for r in rows)                   # 外層表格的列也在，但不混入內層儲存格


def test_fetcher_saves_raw_and_retries(tmp_path):
    f = fetcher([("x.com/a", b"hello")], tmp_path)
    assert f.get("https://x.com/a", {"d": "2026"}) == b"hello"
    assert (tmp_path / "a_2026").read_bytes() == b"hello"
    with pytest.raises(RuntimeError, match="抓取失敗"):
        fetcher([("x.com", ConnectionError("down"))]).get("https://x.com/b")
    assert raw_name("https://h/p/q.json?x=1", {"a": "1/2"}) == "q.json_1-2"


# ------------------------------------------------------------------ 交易日曆
def test_holidays_and_trading_days(tmp_path):
    days = cal.parse_holidays(json.loads(fx("holidaySchedule.json")))
    assert "2026-01-01" in days and "2026-02-12" in days and "2026-10-09" in days
    assert "2026-02-11" not in days                                        # 最後交易日仍交易
    h = set(days)
    assert cal.next_trading_day(date(2026, 10, 8), h) == date(2026, 10, 12)   # 週四 → 跳過週五補假與週末
    assert cal.next_trading_day(date(2026, 10, 23), h) == date(2026, 10, 26)
    assert cal.prev_trading_day(date(2026, 10, 25), h) == date(2026, 10, 23)
    assert cal.is_trading_day(date(2026, 10, 27), h) and not cal.is_trading_day(date(2026, 12, 25), h)
    f = fetcher([("holidaySchedule", fx("holidaySchedule.json"))])
    assert cal.fetch_holidays(f, tmp_path) == days
    got, missing = cal.load_holidays([2026, 2027], tmp_path)
    assert got == h and missing == [2027]
    assert cal.roc_to_date("1151026") == date(2026, 10, 26) and cal.roc_to_date("2026/10/26") == date(2026, 10, 26)


# ------------------------------------------------------------------ 月營收
def test_parse_t21sc03_and_facts(tmp_path):
    sii = revenue.parse_t21sc03(decode(fx("mops_t21sc03_sii_202609.html")), "sii", 2026, 9)
    assert set(sii["ticker"]) == {"2330", "2317", "2454"}                  # 名單外 9999、合計列排除
    r = sii.set_index("ticker")
    assert r.at["2330", "revenue"] == 330_000_000 and pd.isna(r.at["2454", "prev_month"]) and r.at["2454", "note"] == "新上市"
    with pytest.raises(ValueError, match="格式"):
        revenue.parse_t21sc03("<html>維護中</html>", "sii", 2026, 9)
    assert revenue.revenue_url("otc", 2026, 9).endswith("/otc/t21sc03_115_9_0.html")
    # 歷史：先放 24 個月較低的營收 → 2330 創新高、2317 未創（歷史有 800,000,000）
    for k in range(24):
        y, m = (2024 + (8 + k) // 12, (8 + k) % 12 + 1)
        hist = pd.DataFrame({"ym": [f"{y}-{m:02d}"] * 2, "market": ["sii"] * 2, "ticker": ["2330", "2317"],
                             "name": ["台積電", "鴻海"], "revenue": [300_000_000.0, 800_000_000.0], "prev_month": [None] * 2,
                             "last_year": [None] * 2, "cum": [None] * 2, "cum_last_year": [None] * 2, "note": [""] * 2})
        revenue.merge_store(hist, datetime(2026, 1, 1, tzinfo=TZ), tmp_path)
    t1 = datetime(2026, 10, 8, 20, 30, tzinfo=TZ)
    merged = revenue.merge_store(sii, t1, tmp_path)
    again = revenue.merge_store(sii, datetime(2026, 10, 9, 20, 30, tzinfo=TZ), tmp_path)   # first_seen 不變
    assert set(again["first_seen"]) == {t1.isoformat()}
    f = revenue.facts(again, revenue.load_history(tmp_path))
    assert f["2330"].is_new_high is True and f["2317"].is_new_high is False and f["2454"].is_new_high is None
    assert f["2330"].yoy_pct == pytest.approx(32.0) and f["2330"].mom_pct == pytest.approx(10.0)
    assert f["2454"].mom_pct is None and f["2330"].history_months == 24
    ev = revenue.fact_event(f["2330"], "https://mops.example/sii")
    assert ev["basis"] == "monthly_revenue" and ev["authority"] == "mops" and ev["ticker"] == "2330"
    assert ev["content_as_of"] == "2026-09-30T23:59:59+08:00" and ev["published_at"] == t1.isoformat()
    assert "創近 24 個月新高" in ev["statement"] and ev["values"]["is_new_high"] == 1 and ev["values"]["yoy_pct"] == 32.0
    assert "新高" not in revenue.fact_event(f["2454"], "u")["statement"]
    assert revenue.last_reported_month(date(2026, 1, 8)) == (2025, 12)
    del merged


def test_collect_revenue_degrades_per_market(tmp_path):
    f = fetcher([("sii/t21sc03_115_9_0", fx("mops_t21sc03_sii_202609.html")), ("otc", ConnectionError("x"))], tmp_path / "raw")
    facts, urls, warns = revenue.collect_revenue(f, 2026, 9, fetched_at=datetime(2026, 10, 8, 20, tzinfo=TZ),
                                                 base=tmp_path / "rev")
    assert set(facts) == {"2330", "2317", "2454"} and list(urls) == ["sii"] and "otc" in warns[0]
    assert (tmp_path / "raw" / "mops_t21sc03_sii_202609.html").exists()


# ------------------------------------------------------------------ 重大訊息
def test_parse_news_and_cutoff():
    items = news.parse_news(json.loads(fx("news_twse_20261026.json")))
    assert [(n["ticker"], n["time"]) for n in items] == [("2454", "17:00:00"), ("2330", "14:30:05"), ("2317", "20:15:00")]
    ok, late = news.select_day(items, date(2026, 10, 26))
    assert [n["ticker"] for n in ok] == ["2330"] and [n["ticker"] for n in late] == ["2317"]
    ev = news.news_event(ok[0], news.TWSE_NEWS_URL)
    assert ev["basis"] == "material_news" and ev["authority"] == "mops" and ev["content_as_of"] == "2026-10-26T14:30:05+08:00"
    assert "董事會決議資本預算" in ev["statement"] and ev["values"] == {"announce_hhmm": 1430}


def test_collect_news_both_markets():
    f = fetcher([("t187ap04_L", fx("news_twse_20261026.json")), ("t187ap04_O", fx("news_tpex_20261026.json"))])
    evs, warns = news.collect_news(f, date(2026, 10, 26))
    assert {e["ticker"] for e in evs} == {"2330", "8299"} and any("晚於" in w and "2317" in w for w in warns)
    evs, warns = news.collect_news(fetcher([("t187ap04_L", fx("news_twse_20261026.json"))]), date(2026, 10, 26))
    assert {e["ticker"] for e in evs} == {"2330"} and any("上櫃" in w and "失敗" in w for w in warns)


def test_tpex_news_html_response_is_a_warning_not_a_failure():
    html = fx("news_tpex_html_error.html")
    # 第一個上櫃端點回 HTML → 單行警告附前 200 字；第二個端點正常 → 照樣取得上櫃事件
    routes = [("t187ap04_L", fx("news_twse_20261026.json")), ("v1/t187ap04_O", html),
              ("mopsfe_t187ap04_O", fx("news_tpex_20261026.json"))]
    evs, warns = news.collect_news(fetcher(routes), date(2026, 10, 26))
    assert {e["ticker"] for e in evs} == {"2330", "8299"}
    w = [x for x in warns if "不是 JSON" in x]
    assert len(w) == 1 and "上櫃" in w[0] and "前 200 字" in w[0] and "<!DOCTYPE html>" in w[0] and "\n" not in w[0]
    assert len(w[0].split("前 200 字：", 1)[1]) <= 201
    # 兩個上櫃端點都回 HTML → 只有警告，上市事件不受影響
    evs, warns = news.collect_news(fetcher([("t187ap04_L", fx("news_twse_20261026.json")), ("t187ap04_O", html)]),
                                   date(2026, 10, 26))
    assert {e["ticker"] for e in evs} == {"2330"} and sum("不是 JSON" in x for x in warns) == 2
    data, w = news.load_json_list(b'{"data": [{"a": 1}]}', "x")
    assert data == [{"a": 1}] and w is None
    assert news.load_json_list(b'{"msg": "no"}', "x")[1] == "x 回應不是 JSON 陣列"


REAL = FX / "real"


@pytest.mark.parametrize("path", sorted(REAL.glob("news_tpex*")) if REAL.exists() else [], ids=lambda p: p.name)
def test_real_tpex_news_files(path):
    """tests/fixtures/collect/real/news_tpex*（SS 在 Mac 上以 --save-raw 存下的真實回應）：JSON 能解析成事件；
    HTML 等非 JSON 只產生一則單行警告，不會丟例外。"""
    data, w = news.load_json_list(path.read_bytes(), "重大訊息（上櫃）")
    if data is None:
        assert w and "前 200 字" in w and "\n" not in w
    else:
        items = news.parse_news(data)
        assert all(n["ticker"] and n["subject"] and len(n["time"]) == 8 for n in items)


# ------------------------------------------------------------------ 台指期夜盤
def test_parse_taifex_night_session():
    n = taifex.parse_fut_csv(decode(fx("taifex_TX_20261027.csv")))
    assert n["session_date"] == "2026-10-27" and n["contract_month"] == "202611"
    assert n["close"] == 23050 and n["chg_pct"] == pytest.approx(-2.33) and n["volume"] == 52000
    inp = taifex.taifex_input(n)
    assert inp["content_as_of"] == "2026-10-27T05:00:00+08:00" and inp["chg_pct"] == pytest.approx(-2.33)
    day_only = "\n".join(decode(fx("taifex_TX_20261027.csv")).splitlines()[:2])
    assert taifex.parse_fut_csv(day_only) is None
    tx, warns = taifex.collect_taifex(fetcher([("futDataDown", fx("taifex_TX_20261027.csv"))]), date(2026, 10, 20), date(2026, 10, 27))
    assert tx["close"] == 23050 and warns == []
    tx, warns = taifex.collect_taifex(fetcher([]), date(2026, 10, 20), date(2026, 10, 27))
    assert tx is None and "失敗" in warns[0]


# ------------------------------------------------------------------ 美股
def test_parse_us_sources():
    s = us.parse_fred(fx("fred_SP500.csv").decode())
    assert s[-1] == ("2026-10-26", 6470.80) and ("2026-10-23", ".") not in s and len(s) == 3     # 休市 "." 略過
    c = us.last_change(s)
    assert c["date"] == "2026-10-26" and c["chg_pct"] == pytest.approx((6470.80 / 6533.15 - 1) * 100)
    assert us.parse_fred(fx("fred_NASDAQ100_olddate.csv").decode())[-1] == ("2026-10-26", 23859.4)   # 舊版欄名 DATE
    with pytest.raises(ValueError):
        us.parse_fred("<html>error</html>")
    n = us.parse_nasdaq(json.loads(fx("nasdaq_SOX.json")))
    assert n[0][0] == "2026-10-22" and n[-1] == ("2026-10-26", 5238.0)                            # 由舊到新
    with pytest.raises(ValueError, match="Nasdaq"):
        us.parse_nasdaq(json.loads(fx("nasdaq_empty.json")))
    y = us.parse_yahoo(json.loads(fx("yahoo_tsm.json")))
    assert len(y) == 2 and us.last_change(y)["close"] == 306.0                                    # None 收盤略過
    assert us.close_time_taipei("2026-10-26") == "2026-10-27T04:00:00+08:00"   # 夏令（美國 11/1 才結束夏令時間）
    assert us.close_time_taipei("2026-11-02") == "2026-11-03T05:00:00+08:00"   # 冬令


REAL = "real/"


def fred_csv(sid, *rows):
    return (f"observation_date,{sid}\n" + "".join(f"{d},{v}\n" for d, v in rows)).encode()


def test_parse_real_nasdaq_and_cnbc():
    sox = us.parse_nasdaq(json.loads(fx(REAL + "nasdaq_SOX_20261016.json")))
    assert sox[-2:] == [("2026-10-15", 11581.22), ("2026-10-16", 11137.29)] and sox[0][0] == "2026-10-05"   # 千分位逗號
    tsm = us.parse_nasdaq(json.loads(fx(REAL + "nasdaq_TSM_20261016.json")))
    assert tsm[-2:] == [("2026-10-15", 422.25), ("2026-10-16", 412.61)]                                     # $ 符號
    c = us.last_change(tsm)
    assert c["date"] == "2026-10-16" and c["chg_pct"] == pytest.approx((412.61 / 422.25 - 1) * 100)
    with pytest.raises(ValueError, match="rCode 400"):
        us.parse_nasdaq({"data": None, "message": None, "status": {"rCode": 400, "bCodeMessage": [{"code": 1001, "errorMessage": "Invalid"}]}})
    q = us.parse_cnbc(json.loads(fx(REAL + "cnbc_SOX_TSM_20261016.json")))
    assert q[".SOX"] == {"date": "2026-10-16", "close": 11137.287, "chg_pct": -3.83}
    assert q["TSM"]["close"] == 412.61 and q["TSM"]["chg_pct"] == -2.28 and q["TSM"]["date"] == "2026-10-16"
    no_pct = {"FormattedQuoteResult": {"FormattedQuote": [{"symbol": "TSM", "last": "412.61", "last_time": "2026-10-16T16:00:00.000-0400",
                                                           "previous_day_closing": "422.25"}]}}
    assert us.parse_cnbc(no_pct)["TSM"]["chg_pct"] == pytest.approx((412.61 / 422.25 - 1) * 100)
    with pytest.raises(ValueError, match="CNBC"):
        us.parse_cnbc({"FormattedQuoteResult": {"FormattedQuote": []}})


def test_collect_us_nasdaq_first_with_required_params_headers_and_page_urls(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    routes = [("id=SP500", fred_csv("SP500", ("2026-10-15", 6600), ("2026-10-16", 6534))),
              ("id=NASDAQ100", fred_csv("NASDAQ100", ("2026-10-14", 24000), ("2026-10-15", 24100))),   # FRED 晚一天
              ("quote/SOX/historical", fx(REAL + "nasdaq_SOX_20261016.json")),
              ("quote/TSM/historical", fx(REAL + "nasdaq_TSM_20261016.json"))]
    f = fetcher(routes)
    d, warns = us.collect_us(f, not_after=date(2026, 10, 17))
    assert warns == [] and set(d["quotes"]) == {"SOX", "NDX", "SPX", "TSM"}
    assert d["info"] and "NDX" in d["info"][0] and "晚一天" in d["info"][0]                    # 只記 info，不算警告
    calls, kws = f.session.calls, f.session.kw
    assert not any(h in c for c in calls for h in ("yahoo", "stooq"))
    assert sum("quote.cnbc.com" in c for c in calls) == 1                                       # 只為交叉比對抓一次（失敗不影響）
    nq = [(c, kw) for c, kw in zip(calls, kws) if "api.nasdaq.com" in c]
    assert any("quote/SOX/historical?assetclass=index&fromdate=2026-10-03&todate=2026-10-17&limit=10" in c for c, _ in nq)
    assert any("quote/TSM/historical?assetclass=stocks&fromdate=2026-10-03&todate=2026-10-17" in c for c, _ in nq)
    for _, kw in nq:
        h = kw["headers"]
        assert h["Accept"] == "application/json, text/plain, */*" and "Mozilla/5.0" in h["User-Agent"]
        assert h["Origin"] == "https://www.nasdaq.com" and h["Referer"] == "https://www.nasdaq.com/"
    srcs = {s["name"].split()[0]: s for s in d["sources"]}
    assert srcs["SOX"]["url"] == "https://www.nasdaq.com/market-activity/index/sox/historical"
    assert srcs["TSM"]["url"] == "https://www.nasdaq.com/market-activity/stocks/tsm/historical"
    assert not any("api." in s["url"] or "quote.cnbc.com" in s["url"] for s in d["sources"])
    assert srcs["SOX"]["content_as_of"] == "2026-10-17T04:00:00+08:00"                         # 美東 10/16 16:00
    assert srcs["NDX"]["content_as_of"] == "2026-10-16T04:00:00+08:00"                         # 該筆自己的收盤日
    assert d["content_as_of"] == "2026-10-17T04:00:00+08:00"
    assert d["quotes"]["SOX"]["chg_pct"] == pytest.approx((11137.29 / 11581.22 - 1) * 100, abs=1e-3)


def test_collect_us_falls_back_to_cnbc_then_yahoo(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    bad = json.dumps({"data": None, "message": None, "status": {"rCode": 400, "bCodeMessage": None}}).encode()
    routes = [("id=", fred_csv("X", ("2026-10-15", 1), ("2026-10-16", 2))),
              ("api.nasdaq.com", bad), ("quote.cnbc.com", fx(REAL + "cnbc_SOX_TSM_20261016.json"))]
    f = fetcher(routes)
    d, warns = us.collect_us(f, not_after=date(2026, 10, 17))
    assert warns == [] and d["quotes"]["SOX"]["chg_pct"] == -3.83 and d["quotes"]["TSM"]["close"] == 412.61
    assert sum("quote.cnbc.com" in c for c in f.session.calls) == 1                             # 一次查兩個代號
    assert any(".SOX%7CTSM" in c for c in f.session.calls)
    srcs = {s["name"].split()[0]: s["url"] for s in d["sources"]}
    assert srcs["SOX"] == "https://www.cnbc.com/quotes/.SOX" and srcs["TSM"] == "https://www.cnbc.com/quotes/TSM"
    routes = [("id=", fred_csv("X", ("2026-10-15", 1), ("2026-10-16", 2))), ("api.nasdaq.com", bad),
              ("quote.cnbc.com", ConnectionError("x")), ("chart/TSM", [429, fx("yahoo_tsm.json")])]
    f = fetcher(routes)
    d, warns = us.collect_us(f, not_after=date(2026, 10, 27))
    assert "TSM" in d["quotes"] and "SOX" not in d["quotes"]
    assert any(w.startswith("SOX 全部來源失敗") and "Nasdaq" in w and "CNBC" in w and "Yahoo" in w for w in warns)
    assert sum("chart/TSM" in c for c in f.session.calls) == 2                                  # 429 退避後成功
    assert {s["name"].split()[0]: s["url"] for s in d["sources"]}["TSM"] == "https://finance.yahoo.com/quote/TSM/history/"
    assert not any("stooq" in c for c in f.session.calls)


def test_us_fred_timeout_retries_and_total_failure(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    routes = [("id=SP500", [ConnectionError("timeout"), ConnectionError("timeout"), fx("fred_SP500.csv")]),
              ("id=NASDAQ100", ConnectionError("timeout")), ("quote/SOX/historical", fx("nasdaq_SOX.json"))]
    f = fetcher(routes)
    d, warns = us.collect_us(f, not_after=date(2026, 10, 27))
    calls, kws = f.session.calls, f.session.kw
    fredc = [kw for c, kw in zip(calls, kws) if "fred.stlouisfed.org" in c]
    assert fredc and all(kw["timeout"] == 60 for kw in fredc)
    assert sum("id=SP500" in c for c in calls) == 3 and "SPX" in d["quotes"]            # 失敗 2 次後第 3 次成功
    assert sum("id=NASDAQ100" in c for c in calls) == 3                                 # 首次＋重試 2 次
    assert any(w.startswith("NDX 全部來源失敗") for w in warns) and all("\n" not in w for w in warns)
    none, warns = us.collect_us(fetcher([]))
    assert none is None and any("全部抓取失敗" in w for w in warns)


def test_us_date_mismatch_other_than_fred_lag_is_warning():
    routes = [("id=", fred_csv("X", ("2026-10-12", 1), ("2026-10-13", 2))),                      # 晚三天：不是 FRED 慣常延遲
              ("quote/SOX/historical", fx(REAL + "nasdaq_SOX_20261016.json")),
              ("quote/TSM/historical", fx(REAL + "nasdaq_TSM_20261016.json"))]
    d, warns = us.collect_us(fetcher(routes), not_after=date(2026, 10, 17))
    assert d["info"] == [] and any("日期不一致" in w and "NDX" in w for w in warns)


def nasdaq_upto(name, last_date):
    """真實 Nasdaq 回應，只保留 ≤ last_date（MM/DD/YYYY 比較用 ISO）的列。"""
    d = json.loads(fx(REAL + name))
    keep = []
    for r in d["data"]["tradesTable"]["rows"]:
        m, dd, y = r["date"].split("/")
        if f"{y}-{m}-{dd}" <= last_date:
            keep.append(r)
    d["data"]["tradesTable"]["rows"] = keep
    return json.dumps(d).encode()


SS_NOW = datetime(2026, 10, 17, 17, 21, tzinfo=TZ)                    # 台北 10/17 17:21＝美東 10/17 05:21（週六）


def test_expected_us_close():
    assert us.expected_us_close(SS_NOW) == date(2026, 10, 16)
    assert us.expected_us_close(datetime(2026, 10, 17, 3, 59, tzinfo=TZ)) == date(2026, 10, 15)   # 美東 10/16 15:59 未收盤
    assert us.expected_us_close(datetime(2026, 10, 17, 4, 0, tzinfo=TZ)) == date(2026, 10, 16)
    assert us.expected_us_close(datetime(2026, 10, 19, 20, 30, tzinfo=TZ)) == date(2026, 10, 16)  # 週一晚上：上週五
    assert us.expected_us_close(datetime(2026, 11, 27, 6, 50, tzinfo=TZ)) == date(2026, 11, 25)   # 感恩節休市


def test_stale_nasdaq_sox_falls_through_to_cnbc():
    """實測：Nasdaq SOX 只到 10/15（11,581.22，−0.88%），CNBC 有 10/16（11,137.29，−3.83%）→ 採用 CNBC。"""
    routes = [("id=", fred_csv("X", ("2026-10-15", 1), ("2026-10-16", 2))),
              ("quote/SOX/historical", nasdaq_upto("nasdaq_SOX_20261016.json", "2026-10-15")),
              ("quote/TSM/historical", fx(REAL + "nasdaq_TSM_20261016.json")),
              ("quote.cnbc.com", fx(REAL + "cnbc_SOX_TSM_20261016.json"))]
    f = fetcher(routes)
    d, warns = us.collect_us(f, not_after=SS_NOW.date(), now=SS_NOW)
    assert warns == []
    assert d["quotes"]["SOX"] == {"close": 11137.287, "chg_pct": -3.83}
    srcs = {s["name"].split()[0]: s for s in d["sources"]}
    assert "CNBC" in srcs["SOX"]["name"] and srcs["SOX"]["content_as_of"] == "2026-10-17T04:00:00+08:00"
    assert "Nasdaq" in srcs["TSM"]["name"]                                                     # TSM 的 Nasdaq 是新的
    assert sum("quote.cnbc.com" in c for c in f.session.calls) == 1 and not any("yahoo" in c for c in f.session.calls)


def test_all_sources_stale_uses_newest_with_warning():
    routes = [("id=", fred_csv("X", ("2026-10-15", 1), ("2026-10-16", 2))),
              ("quote/SOX/historical", nasdaq_upto("nasdaq_SOX_20261016.json", "2026-10-15")),
              ("quote/TSM/historical", fx(REAL + "nasdaq_TSM_20261016.json")),
              ("quote.cnbc.com", ConnectionError("x")), ("chart/%5ESOX", ConnectionError("x"))]
    d, warns = us.collect_us(fetcher(routes), not_after=SS_NOW.date(), now=SS_NOW)
    assert d["quotes"]["SOX"]["close"] == 11581.22
    assert d["quotes"]["SOX"]["chg_pct"] == pytest.approx((11581.22 / 11684.38 - 1) * 100, abs=1e-4)   # −0.88%
    w = [x for x in warns if x.startswith("SOX 所有來源的收盤都早於預期 2026-10-16")]
    assert len(w) == 1 and "Nasdaq 2026-10-15" in w[0] and "CNBC" in w[0]
    assert {s["name"].split()[0]: s["content_as_of"] for s in d["sources"]}["SOX"] == "2026-10-16T04:00:00+08:00"


def test_cross_source_change_divergence_warns():
    cn = json.loads(fx(REAL + "cnbc_SOX_TSM_20261016.json"))
    for q in cn["FormattedQuoteResult"]["FormattedQuote"]:
        if q["symbol"] == "TSM":
            q["change_pct"] = "-4.50%"                                       # Nasdaq 算出 −2.28%
    routes = [("id=", fred_csv("X", ("2026-10-15", 1), ("2026-10-16", 2))),
              ("quote/SOX/historical", fx(REAL + "nasdaq_SOX_20261016.json")),
              ("quote/TSM/historical", fx(REAL + "nasdaq_TSM_20261016.json")),
              ("quote.cnbc.com", json.dumps(cn).encode())]
    d, warns = us.collect_us(fetcher(routes), not_after=SS_NOW.date(), now=SS_NOW)
    assert d["quotes"]["TSM"]["close"] == 412.61                                             # 仍採用 Nasdaq
    assert [w for w in warns if "差異過大" in w] == [
        "TSM 2026-10-16 日漲跌兩來源差異過大：Nasdaq -2.28%、CNBC -4.50%（> 1 個百分點）"]
    # SOX：Nasdaq −3.833% vs CNBC −3.83% → 不警告；CNBC 日期不同時不比對
    routes[3] = ("quote.cnbc.com", fx(REAL + "cnbc_SOX_TSM_20261016.json"))
    _, warns = us.collect_us(fetcher(routes), not_after=SS_NOW.date(), now=SS_NOW)
    assert not any("差異過大" in w for w in warns)


def test_us_and_taifex_inputs_become_observations():
    from esun_agent.agent import observations as ob
    from esun_agent.data.prices import load_panel
    panel = load_panel()
    D = panel.dates[-1]
    d, _ = us.collect_us(fetcher([("id=", fx("fred_SP500.csv")), ("yahoo", fx("yahoo_tsm.json"))]), not_after=date(2026, 10, 27))
    tx = taifex.taifex_input(taifex.parse_fut_csv(decode(fx("taifex_TX_20261027.csv"))))
    ev = ob.build_evidence(panel, D, inputs={"us": d, "taifex": tx, "events": [
        news.news_event(news.parse_news(json.loads(fx("news_twse_20261026.json")))[1], news.TWSE_NEWS_URL)]})
    o = ev.by_topic("us_overnight")[0]
    assert len(o["source_ref"]) == 4 and all(s["authority"] == "vendor" for s in ev.sources if s["source_id"] in o["source_ref"])
    assert ev.by_topic("taifex_night")[0]["values"]["chg_pct"] == pytest.approx(-2.33)
    mn = ev.by_topic("material_news")[0]
    assert ev.meta[mn["obs_id"]]["basis"] == "material_news" and ev.meta[mn["obs_id"]]["ticker"] == "2330"


def test_collect_day_degrades_each_step(tmp_path, monkeypatch):
    from esun_agent.collect import daily, revenue as rv
    monkeypatch.setattr(rv, "REVENUE_DIR", tmp_path / "rev")
    routes = [("sii/t21sc03_115_9_0", fx("mops_t21sc03_sii_202609.html")), ("otc/t21sc03", fx("mops_t21sc03_otc_202609.html")),
              ("t187ap04_L", fx("news_twse_20261026.json")), ("t187ap04_O", ConnectionError("x")),
              ("futDataDown", fx("taifex_TX_20261027.csv")), ("id=", ConnectionError("x")), ("yahoo", ConnectionError("x")),
              ("nasdaq", ConnectionError("x")), ("stooq", ConnectionError("x"))]
    r = daily.collect_day(date(2026, 10, 26), tmp_path / "raw", steps=("revenue", "news", "taifex", "us"),
                          now=datetime(2026, 10, 26, 20, 30, tzinfo=TZ), do_archive=False, fetcher=fetcher(routes, tmp_path / "raw"))
    assert set(r.done) == {"revenue", "news", "taifex", "us"} and not r.market_ok
    assert set(r.revenue_events) == {"2330", "2317", "2454", "8299"}
    assert r.inputs["taifex"]["close"] == 23050 and "us" not in r.inputs
    assert [e["ticker"] for e in r.inputs["events"]] == ["2330"]
    assert any("美股" in w for w in r.warnings) and any("上櫃" in w for w in r.warnings)
    assert (tmp_path / "raw" / "taifex_TX_20261026.csv").exists()               # 以執行日命名


def test_collect_data_cli(monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("cd", Path(__file__).parent.parent / "scripts" / "collect_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert list(mod.months("2025-11", (2026, 2))) == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]
    seen = []
    monkeypatch.setattr(mod, "collect_revenue", lambda f, y, m, fetched_at: (seen.append((y, m)) or ({}, {"sii": "u"}, [])))
    monkeypatch.setattr(mod, "last_reported_month", lambda d: (2026, 2))
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    assert mod.main(["revenue-backfill", "--start", "2026-01"]) == 0
    assert seen == [(2026, 1), (2026, 2)] and "完成 2 個月份" in capsys.readouterr().out


def test_etf_carry_forward_keeps_missing_etfs(tmp_path):
    from esun_agent.collect.daily import carry_forward_etf
    old = {"as_of": "2026-10-23", "data_dates": {"A": "2026-10-22", "B": "2026-10-22"},
           "top15": {"A": {"2330": 0.1}, "B": {"2317": 0.1}}}
    new = {"as_of": "2026-10-26", "data_dates": {"A": "2026-10-26"}, "top15": {"A": {"2330": 0.12}}}
    (tmp_path / "2026-10-23.json").write_text(json.dumps(old))
    (tmp_path / "2026-10-26.json").write_text(json.dumps(new))
    assert carry_forward_etf("2026-10-26", ["B", "C"], tmp_path) == ["B"]
    got = json.loads((tmp_path / "2026-10-26.json").read_text())
    assert got["top15"]["B"] == {"2317": 0.1} and got["top15"]["A"] == {"2330": 0.12}
    assert got["data_dates"]["B"] == "2026-10-22" and got["carried_forward"] == {"B": "2026-10-22"}
    assert carry_forward_etf("2026-10-27", ["B"], tmp_path) == []          # 當日檔不存在
