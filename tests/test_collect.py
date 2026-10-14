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


def fx(name) -> bytes:
    return (FX / name).read_bytes()


class FakeResp:
    def __init__(self, content=b"", status=200, headers=None):
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
        self.routes, self.headers, self.calls = routes, {}, []

    def _hit(self, url, **kw):
        self.calls.append(url)
        for frag, r in self.routes:
            if frag in url:
                if isinstance(r, Exception):
                    import requests
                    raise requests.ConnectionError(str(r))
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
    s = us.parse_stooq(fx("stooq_sox.csv").decode())
    c = us.last_change(s)
    assert c["date"] == "2026-10-26" and c["chg_pct"] == pytest.approx((5238 / 5400 - 1) * 100)
    assert us.last_change(s, date(2026, 10, 25))["date"] == "2026-10-23"
    with pytest.raises(ValueError):
        us.parse_stooq("No data")
    y = us.parse_yahoo(json.loads(fx("yahoo_tsm.json")))
    assert len(y) == 2 and us.last_change(y)["close"] == 306.0                # None 收盤略過
    assert us.close_time_taipei("2026-10-26") == "2026-10-27T04:00:00+08:00"   # 夏令（美國 11/1 才結束夏令時間）
    assert us.close_time_taipei("2026-11-02") == "2026-11-03T05:00:00+08:00"   # 冬令
    assert us.close_time_taipei("2026-07-15") == "2026-07-16T04:00:00+08:00"   # 夏令


def test_collect_us_falls_back_and_degrades():
    routes = [("s=^sox", fx("stooq_sox.csv")), ("s=^ndx", fx("stooq_nodata.csv")), ("%5ENDX", fx("yahoo_tsm.json")),
              ("s=^spx", ConnectionError("x")), ("%5EGSPC", ConnectionError("x")), ("tsm.us", fx("stooq_sox.csv"))]
    d, warns = us.collect_us(fetcher(routes), not_after=date(2026, 10, 27))
    assert set(d["quotes"]) == {"SOX", "NDX", "TSM"} and any("SPX 兩個來源都失敗" in w for w in warns)
    assert "yahoo" in d["sources"][1]["url"] and len(d["sources"]) == 3
    assert d["content_as_of"].endswith("+08:00")
    none, warns = us.collect_us(fetcher([]))
    assert none is None and any("全部抓取失敗" in w for w in warns)


def test_us_and_taifex_inputs_become_observations():
    from esun_agent.agent import observations as ob
    from esun_agent.data.prices import load_panel
    panel = load_panel()
    D = panel.dates[-1]
    d, _ = us.collect_us(fetcher([("s=^", fx("stooq_sox.csv")), ("tsm.us", fx("stooq_sox.csv"))]), not_after=date(2026, 10, 27))
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
              ("futDataDown", fx("taifex_TX_20261027.csv")), ("s=^", ConnectionError("x")), ("%5E", ConnectionError("x")),
              ("tsm.us", ConnectionError("x")), ("TSM", ConnectionError("x"))]
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
