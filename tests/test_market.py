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
    assert "114/01/02" in m.source_info("2025-01-02", "tpex", historical=True)["url"]
    assert m.source_info("2025-12-31", "twse", kind="institutional", as_of="2025-12-31T17:00:00+08:00")[
        "content_as_of"].endswith("17:00:00+08:00")
    with pytest.raises(ValueError):
        m.source_info("2025-12-31", "taifex")


def test_sources_for_day_uses_real_universe():
    tpex_ticker = next(t for t, v in m.load_universe().items() if v["market"] == "TPEX")
    df = pd.DataFrame({"date": ["2025-12-31"] * 2, "ticker": ["2330", tpex_ticker]})
    assert [s["authority"] for s in m.sources_for_day(df)] == ["twse", "tpex"]


class FakeFetcher(m.MarketFetcher):
    def __init__(self):
        self.calls = 0

    def twse_stock_month(self, ticker, y, mo):
        self.calls += 1
        return m.parse_twse_stock_day(fx("twse_stock_day_2330.json"), ticker) if (y, mo) == (2025, 1) \
            else pd.DataFrame(columns=m.MARKET_COLUMNS)

    def tpex_day(self, d):
        return m.parse_tpex_history_day(fx("tpex_history_day.json"), d, UNI)

    def twse_inst(self, d):
        return m.parse_twse_t86(fx("twse_t86.json"), d, UNI)

    def tpex_inst(self, d):
        return m.parse_tpex_inst_history_day(fx("tpex_inst_history_day.json"), d, UNI)


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
