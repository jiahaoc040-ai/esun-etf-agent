import json
import math
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from esun_agent.data import market as m

FX = Path(__file__).parent / "fixtures" / "market"
UNI = {"2330": {"name": "台積電", "market": "TWSE"}, "2883": {"name": "凱基金", "market": "TWSE"},
       "2454": {"name": "聯發科", "market": "TWSE"}, "3443": {"name": "創意", "market": "TPEX"},
       "6488": {"name": "環球晶", "market": "TPEX"}}


def fx(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def test_to_number_and_dates():
    assert m.to_number("1,234.5") == 1234.5
    assert math.isnan(m.to_number("--")) and math.isnan(m.to_number(None)) and math.isnan(m.to_number(""))
    assert m.parse_roc_date("1141231") == date(2025, 12, 31)
    assert m.parse_roc_date("114/01/02") == date(2025, 1, 2)
    assert m.parse_roc_date("20251231") == date(2025, 12, 31)
    assert m.to_roc_slash(date(2025, 1, 2)) == "114/01/02"
    with pytest.raises(ValueError):
        m.parse_roc_date("abc")


def test_twse_day_all_filters_and_avg_price():
    df = m.parse_twse_day_all(fx("twse_day_all.json"), UNI)
    assert list(df.columns) == m.MARKET_COLUMNS
    assert list(df["ticker"]) == ["2330", "2883"]  # 0050 不在名單、2454 無成交
    r = df.iloc[0]
    assert r["date"] == "2025-12-31" and r["close"] == 1205.0 and r["volume"] == 30_000_000
    assert r["avg_price"] == pytest.approx(1200.0)


def test_twse_stock_day_month():
    df = m.parse_twse_stock_day(fx("twse_stock_day_2330.json"), "2330")
    assert list(df["date"]) == ["2025-01-02", "2025-01-03"]
    assert df.iloc[0]["avg_price"] == pytest.approx(600.0)
    assert df.iloc[1]["avg_price"] == pytest.approx(605.0)
    assert m.parse_twse_stock_day(fx("twse_stock_day_empty.json"), "2330").empty


def test_tpex_day_all_and_history():
    df = m.parse_tpex_day_all(fx("tpex_day_all.json"), UNI)
    assert list(df["ticker"]) == ["3443"]  # 6488 停牌、9999 不在名單
    assert df.iloc[0]["avg_price"] == pytest.approx(2000.5)
    h = m.parse_tpex_history_day(fx("tpex_history_day.json"), date(2025, 1, 2), UNI)
    assert list(h["ticker"]) == ["3443"] and h.iloc[0]["date"] == "2025-01-02"
    assert h.iloc[0]["avg_price"] == pytest.approx(1501.0)
    assert m.parse_tpex_history_day({"tables": []}, date(2025, 1, 2), UNI).empty


def test_t86():
    df = m.parse_twse_t86(fx("twse_t86.json"), date(2025, 12, 31), UNI)
    assert list(df["ticker"]) == ["2330", "2883"]
    r = df.iloc[0]
    assert r["foreign_net"] == 5_000_000 - 100_000  # 外陸資 + 外資自營商
    assert r["trust_net"] == 300_000 and r["dealer_net"] == -200_000 and r["total_net"] == 5_000_000
    assert m.parse_twse_t86(fx("twse_t86_empty.json"), date(2025, 12, 31), UNI).empty


def test_tpex_inst():
    a = m.parse_tpex_inst_all(fx("tpex_inst_all.json"), UNI)
    r = a.iloc[0]
    assert (r["foreign_net"], r["trust_net"], r["dealer_net"], r["total_net"]) == (-50_000, 20_000, -3_000, -33_000)
    h = m.parse_tpex_inst_history_day(fx("tpex_inst_history_day.json"), date(2025, 1, 2), UNI)
    assert list(h["ticker"]) == ["3443"]
    r = h.iloc[0]
    assert (r["foreign_net"], r["trust_net"], r["dealer_net"], r["total_net"]) == (-50_000, 20_000, -3_000, -33_000)
    with pytest.raises(KeyError):
        m.parse_tpex_inst_all([{"Date": "1141231", "SecuritiesCompanyCode": "3443", "X": "1"}], UNI)


def test_save_load_roundtrip_and_coverage(tmp_path):
    df = pd.concat([m.parse_twse_day_all(fx("twse_day_all.json"), UNI),
                    m.parse_tpex_day_all(fx("tpex_day_all.json"), UNI)], ignore_index=True)
    paths = m.save_daily(df, tmp_path)
    assert [p.name for p in paths] == ["2025-12-31.parquet"]
    back = m.load_daily("2025-12-31", tmp_path)
    assert list(back.columns) == m.MARKET_COLUMNS and len(back) == 3
    assert m.check_coverage(back, UNI) == ["2454", "6488"]


def test_source_info():
    s = m.source_info("2025-12-31", "twse")
    assert s == {"authority": "twse", "url": m.TWSE_DAY_ALL_URL, "content_as_of": "2025-12-31T13:30:00+08:00"}
    assert m.source_info("2025-12-31", "tpex")["url"] == m.TPEX_DAY_ALL_URL
    assert "date=2025/01/02" in m.source_info("2025-01-02", "tpex", historical=True)["url"]
    assert m.source_info("2025-12-31", "twse", kind="institutional", as_of="2025-12-31T17:00:00+08:00")[
        "content_as_of"].endswith("17:00:00+08:00")
    with pytest.raises(ValueError):
        m.source_info("2025-12-31", "taifex")


def test_sources_for_day_uses_real_universe():
    tpex_ticker = next(t for t, v in m.load_universe().items() if v["market"] == "TPEX")
    df = pd.DataFrame({"date": ["2025-12-31"] * 2, "ticker": ["2330", tpex_ticker]})
    assert [s["authority"] for s in m.sources_for_day(df)] == ["twse", "tpex"]


def test_tpex_history_rejects_wrong_or_missing_date():
    # 回應是 2025-01-02 的資料，但查詢 2025-01-03 → 端點忽略日期參數的情況
    with pytest.raises(ValueError, match="≠ 查詢日期"):
        m.parse_tpex_history_day(fx("tpex_history_day.json"), date(2025, 1, 3), UNI)
    with pytest.raises(ValueError, match="≠ 查詢日期"):
        m.parse_tpex_inst_history_day(fx("tpex_inst_history_day.json"), date(2025, 1, 3), UNI)
    for name, fn in (("tpex_history_day.json", m.parse_tpex_history_day),
                     ("tpex_inst_history_day.json", m.parse_tpex_inst_history_day)):
        payload = fx(name)
        del payload["date"]
        with pytest.raises(ValueError, match="沒有 date"):
            fn(payload, date(2025, 1, 2), UNI)


def test_backfill_does_not_save_on_date_mismatch(tmp_path, monkeypatch):
    uni = {"2330": {"name": "x", "market": "TWSE"}, "3443": {"name": "y", "market": "TPEX"}}
    monkeypatch.setattr(m, "load_universe", lambda: uni)

    class Stale(FakeFetcher):
        def tpex_day(self, d):  # 端點忽略日期參數：永遠回 2025-01-02 的資料
            return m.parse_tpex_history_day(fx("tpex_history_day.json"), d, UNI)

    with pytest.raises(ValueError):
        m.backfill(date(2025, 1, 1), date(2025, 1, 31), Stale(), tmp_path, log=lambda *_: None)
    assert not (tmp_path / "2025-01-03.parquet").exists()


def test_save_raw(tmp_path):
    class R:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    class S:
        headers = {}
        def get(self, url, params=None, timeout=None): return R({"date": "20250102", "tables": []})

    f = m.MarketFetcher(session=S(), raw_dir=tmp_path)
    f.get_json(m.TPEX_DAY_URL, {"date": "2025/01/02", "type": "EW", "response": "json"})
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1 and files[0].name.startswith("otc_2025-01-02")
    assert json.loads(files[0].read_text(encoding="utf-8"))["date"] == "20250102"


class FakeFetcher(m.MarketFetcher):
    def __init__(self):
        self.calls = 0

    def twse_stock_month(self, ticker, y, mo):
        self.calls += 1
        return m.parse_twse_stock_day(fx("twse_stock_day_2330.json"), ticker) if (y, mo) == (2025, 1) \
            else pd.DataFrame(columns=m.MARKET_COLUMNS)

    def tpex_day(self, d):
        return m.parse_tpex_history_day(fx("tpex_history_day.json") | {"date": d.strftime("%Y%m%d")}, d, UNI)

    def twse_inst(self, d):
        return m.parse_twse_t86(fx("twse_t86.json"), d, UNI)

    def tpex_inst(self, d):
        return m.parse_tpex_inst_history_day(fx("tpex_inst_history_day.json") | {"date": d.strftime("%Y%m%d")}, d, UNI)


def test_backfill_resumes(tmp_path, monkeypatch):
    uni = {t: {"name": "x", "market": "TWSE"} for t in ("2330", "2883")} | {"3443": {"name": "y", "market": "TPEX"}}
    monkeypatch.setattr(m, "load_universe", lambda: uni)
    f = FakeFetcher()
    days = m.backfill(date(2025, 1, 1), date(2025, 2, 28), f, tmp_path, inst=True, inst_base=tmp_path / "inst", log=lambda *_: None)
    assert days == ["2025-01-02", "2025-01-03"] and f.calls == 4  # 2 檔 × 2 月
    day = m.load_daily("2025-01-02", tmp_path)
    assert sorted(day["ticker"]) == ["2330", "2883", "3443"]
    inst = m.load_daily("2025-01-02", tmp_path / "inst", kind="institutional")
    assert sorted(inst["ticker"]) == ["2330", "2883", "3443"]
    assert m.backfill(date(2025, 1, 1), date(2025, 2, 28), f, tmp_path, log=lambda *_: None) == []


def test_fetch_latest_rejects_date_mismatch():
    class F(m.MarketFetcher):
        def __init__(self): pass
        def twse_day_all(self): return m.parse_twse_day_all(fx("twse_day_all.json"), UNI)
        def tpex_day_all(self):
            df = m.parse_tpex_day_all(fx("tpex_day_all.json"), UNI)
            df["date"] = "2025-12-30"
            return df
    with pytest.raises(RuntimeError, match="日期不一致"):
        m.fetch_latest(F())


# ------------------------------------------------------------------ 上櫃日行情：讀取中斷重試、依日期查詢備援
class _Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class _Sess:
    """script：依 URL 片段回傳的序列（例外會被 raise）。"""
    def __init__(self, script):
        self.script, self.calls, self.headers = script, [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        for frag, seq in self.script.items():
            if frag in url:
                r = seq.pop(0) if len(seq) > 1 else seq[0]
                if isinstance(r, BaseException):
                    raise r
                return _Resp(r)
        return _Resp({}, 404)


def test_get_json_retries_incomplete_read_with_backoff(monkeypatch):
    import http.client
    import requests
    sleeps = []
    monkeypatch.setattr(m.time, "sleep", sleeps.append)
    s = _Sess({"x.com": [requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead(100 bytes read)"),
                         http.client.IncompleteRead(b"partial"), {"ok": 1}]})
    f = m.MarketFetcher(session=s)
    assert f.get_json("https://x.com/a") == {"ok": 1} and len(s.calls) == 3 and sleeps == [1, 2]
    s2 = _Sess({"x.com": [requests.exceptions.ChunkedEncodingError("broken")]})
    with pytest.raises(RuntimeError, match="3 次.*ChunkedEncodingError"):
        m.MarketFetcher(session=s2).get_json("https://x.com/b")
    assert len(s2.calls) == 3


def test_fetch_latest_falls_back_to_dated_tpex_endpoint(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    real = json.loads((FX / "real" / "otc_2025-01-02_EW_json.json").read_text(encoding="utf-8"))
    twse = [r | {"Date": "1140102"} for r in fx("twse_day_all.json")]
    import requests
    s = _Sess({"STOCK_DAY_ALL": [twse], "tpex_mainboard_daily_close_quotes": [requests.exceptions.ChunkedEncodingError("x")],
               "afterTrading/otc": [real]})
    warns = []
    df = m.fetch_latest(m.MarketFetcher(session=s), expected=date(2025, 1, 2), warnings=warns)
    assert set(df["date"]) == {"2025-01-02"} and (df["ticker"].map(lambda t: m.load_universe()[t]["market"]) == "TPEX").sum() > 10
    url, params = next(c for c in s.calls if "afterTrading/otc" in c[0])
    assert url == m.TPEX_DAY_URL and params == {"date": "2025/01/02", "type": "EW", "response": "json"}
    assert sum("tpex_mainboard" in c[0] for c in s.calls) == 3 and "afterTrading/otc?date=2025/01/02" in warns[0]
    # OpenAPI 成功但是舊日期 → 也改用依日期查詢
    old = [r | {"Date": "1131231", "SecuritiesCompanyCode": "8299"} for r in fx("tpex_day_all.json")[:1]]
    s2 = _Sess({"STOCK_DAY_ALL": [twse], "tpex_mainboard_daily_close_quotes": [old], "afterTrading/otc": [real]})
    w2 = []
    assert set(m.fetch_latest(m.MarketFetcher(session=s2), expected=date(2025, 1, 2), warnings=w2)["date"]) == {"2025-01-02"}
    assert "OpenAPI 日期" in w2[0]


def test_fetch_latest_dated_fallback_must_match_and_market_not_ready(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    real = json.loads((FX / "real" / "otc_2025-01-02_EW_json.json").read_text(encoding="utf-8"))
    twse = [r | {"Date": "1140103"} for r in fx("twse_day_all.json")]
    s = _Sess({"STOCK_DAY_ALL": [twse], "tpex_mainboard": [ValueError("bad json")], "afterTrading/otc": [real]})
    with pytest.raises(ValueError, match="≠ 查詢日期"):                       # 端點回傳 1/2，要求的是 1/3
        m.fetch_latest(m.MarketFetcher(session=s), expected=date(2025, 1, 3))
    s2 = _Sess({"STOCK_DAY_ALL": [twse]})
    with pytest.raises(m.MarketNotReady, match="尚未公布"):
        m.fetch_latest(m.MarketFetcher(session=s2), expected=date(2025, 1, 6))


# ------------------------------------------------------------------ T86 selectType=ALL、MI_INDEX 備援、BFI82U
def test_twse_t86_uses_select_all_and_raises_when_stat_not_ok(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    s = _Sess({"fund/T86": [fx("twse_t86.json")]})
    df = m.MarketFetcher(session=s).twse_inst(date(2025, 12, 31))
    assert not df.empty and s.calls[0][1]["selectType"] == "ALL" and s.calls[0][1]["date"] == "20251231"
    assert "selectType=ALL&" in m.source_info("2025-12-31", "twse", kind="institutional")["url"]
    s2 = _Sess({"fund/T86": [fx("twse_t86_empty.json")]})                    # stat「很抱歉，沒有符合條件的資料!」
    with pytest.raises(ValueError, match="T86 2026-10-16 stat="):
        m.MarketFetcher(session=s2).twse_inst(date(2026, 10, 16))
    assert m.parse_twse_t86(fx("twse_t86_empty.json"), date(2026, 10, 16), UNI).empty


def test_collect_market_warns_when_t86_stat_not_ok(monkeypatch, tmp_path):
    from esun_agent.collect import daily

    class F(m.MarketFetcher):
        def __init__(self): pass
        def twse_day_all(self): return m.parse_twse_day_all([r | {"Date": "1151016"} for r in fx("twse_day_all.json")], UNI)
        def tpex_day_all(self): return m.parse_tpex_day_all([r | {"Date": "1151016"} for r in fx("tpex_day_all.json")], UNI)
        def twse_inst(self, d): raise ValueError(f"T86 {d} stat='很抱歉，沒有符合條件的資料!'")
        def tpex_inst(self, d): return m.parse_tpex_inst_history_day(fx("tpex_inst_history_day.json") | {"date": d.strftime("%Y%m%d")}, d, UNI)
    monkeypatch.setattr(m, "save_daily", lambda *a, **k: [])
    monkeypatch.setattr(m, "load_universe", lambda: UNI)
    warns = daily.collect_market(date(2026, 10, 16), None, fetcher=F())
    assert any("上市 T86 抓取失敗" in w and "沒有符合條件" in w for w in warns)


def test_parse_mi_index_finds_table_by_title_and_strips_html():
    p = fx("twse_mi_index_20261016.json")
    df = m.parse_twse_mi_index(p, date(2026, 10, 16), UNI)
    assert list(df["ticker"]) == ["2330", "2454"]                             # 0050 不在 universe；2883 無成交
    r = df.set_index("ticker").loc["2330"]
    assert r["close"] == 1300.0 and r["volume"] == 25_000_000 and r["avg_price"] == pytest.approx(32_500_000_000 / 25_000_000)
    assert r["date"] == "2026-10-16"
    shuffled = dict(p, tables=list(reversed(p["tables"])))                    # 表的順序改變也找得到
    assert len(m.parse_twse_mi_index(shuffled, date(2026, 10, 16), UNI)) == 2
    with pytest.raises(ValueError, match="≠ 查詢日期"):
        m.parse_twse_mi_index(p, date(2026, 10, 15), UNI)
    with pytest.raises(ValueError, match="stat"):
        m.parse_twse_mi_index({"stat": "很抱歉，沒有符合條件的資料!"}, date(2026, 10, 16), UNI)
    with pytest.raises(KeyError, match="每日收盤行情"):
        m.parse_twse_mi_index(dict(p, tables=p["tables"][:2]), date(2026, 10, 16), UNI)


def test_fetch_latest_falls_back_to_mi_index_when_stock_day_all_is_stale(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "load_universe", lambda: UNI)
    stale = [r | {"Date": "1151015"} for r in fx("twse_day_all.json")]        # 實測 10/17 仍停在 1151015
    tpex = [r | {"Date": "1151016"} for r in fx("tpex_day_all.json")]
    s = _Sess({"STOCK_DAY_ALL": [stale], "afterTrading/MI_INDEX": [fx("twse_mi_index_20261016.json")],
               "tpex_mainboard_daily_close_quotes": [tpex]})
    warns = []
    df = m.fetch_latest(m.MarketFetcher(session=s), expected=date(2026, 10, 16), warnings=warns)
    assert set(df["date"]) == {"2026-10-16"} and {"2330", "2454", "3443"} <= set(df["ticker"])
    url, params = next(c for c in s.calls if "MI_INDEX" in c[0])
    assert url == m.TWSE_MI_INDEX_URL and params == {"date": "20261016", "type": "ALLBUT0999", "response": "json"}
    assert any("STOCK_DAY_ALL 日期為 2026-10-15" in w and "MI_INDEX" in w for w in warns)
    s2 = _Sess({"STOCK_DAY_ALL": [stale], "afterTrading/MI_INDEX": [{"stat": "很抱歉，沒有符合條件的資料!"}]})
    with pytest.raises(m.MarketNotReady, match="MI_INDEX"):
        m.fetch_latest(m.MarketFetcher(session=s2), expected=date(2026, 10, 16))


def test_parse_bfi82u_amounts_in_100m():
    f = m.parse_twse_bfi82u(fx("twse_bfi82u_20261016.json"), date(2026, 10, 16))
    assert f["foreign"] == pytest.approx(-203.12345678) and f["trust"] == pytest.approx(25.0)
    assert f["dealer"] == pytest.approx(10.0 - 25.0) and f["total"] == pytest.approx(-193.07345678)
    with pytest.raises(ValueError, match="≠ 查詢日期"):
        m.parse_twse_bfi82u(fx("twse_bfi82u_20261016.json"), date(2026, 10, 15))
    with pytest.raises(ValueError, match="stat"):
        m.parse_twse_bfi82u({"stat": "很抱歉，沒有符合條件的資料!"}, date(2026, 10, 16))
