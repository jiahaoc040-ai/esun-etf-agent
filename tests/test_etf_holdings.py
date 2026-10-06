import csv
import json
from datetime import date
from pathlib import Path

import pytest

from esun_agent.data import etf_holdings as eh

FX = Path(__file__).parent / "fixtures" / "etf"
EXPECTED = {"2330": 0.095, "2327": 0.081, "LITE.US": 0.062, "009150.KS": 0.055, "2454": 0.05,
            "2317": 0.044, "2308": 0.041, "2382": 0.038, "2345": 0.033, "3443": 0.03}


def read(name):
    return (FX / name).read_text(encoding="utf-8")


def test_parse_holding_name():
    p = eh.parse_holding_name
    assert p("台積電(2330.TW)") == "2330" and p("國巨*(2327.TW)") == "2327"
    assert p("Lumentum(LITE.US)") == "LITE.US" and p("Samsung Elec Mech(009150.KS)") == "009150.KS"
    assert p("X(00679B.TW)") == "00679B.TW"  # .TW 但非 4 位數 → 保留原字串
    assert p("現金") is None and p("") is None


def test_parse_moneydj_sample():
    r = eh.parse_moneydj(read("moneydj_sample.html"), "00981A")
    # 取「持股明細」區塊的 10/05，而不是頁面上更早出現的另一個「資料日期」(10/01)
    assert r["data_date"] == "2026-10-05"
    assert r["holdings"] == EXPECTED


def test_parse_moneydj_failures():
    with pytest.raises(ValueError, match="只解析出 4 檔"):
        eh.parse_moneydj(read("moneydj_short.html"), "x")
    with pytest.raises(ValueError, match="沒有「持股明細」"):
        eh.parse_moneydj(read("moneydj_nodetail.html"), "x")


def test_sources_file():
    src = eh.load_sources()
    assert len(src) == 30
    assert {c for c, s in src.items() if s["overseas"]} == {"00989A", "00997A", "00402A"}
    # 全球型但持有台股者不可跳過
    assert not any(src[c]["overseas"] for c in ("00983A", "00988A", "00409A"))
    assert src["00981A"]["url"] == "https://www.moneydj.com/ETF/X/Basic/Basic0007.xdjhtm?etfid=00981A.TW"


class Resp:
    def __init__(self, text):
        self.text, self.encoding = text, "ISO-8859-1"
    def raise_for_status(self): pass


class Sess:
    headers = {}
    def __init__(self, pages): self.pages, self.resps = pages, []
    def get(self, url, timeout=None):
        if url not in self.pages:
            raise ConnectionError("blocked")
        r = Resp(self.pages[url])
        self.resps.append(r)
        return r


SOURCES = {
    "00981A": {"overseas": False, "url": "http://a"},   # 資料日期 10/05（新）
    "00988A": {"overseas": False, "url": "http://old"},  # 資料日期 10/02（舊）
    "00984A": {"overseas": False, "url": "http://short"},
    "00982A": {"overseas": False, "url": "http://down"},
    "00989A": {"overseas": True, "url": "http://x"},
}
PAGES = {"http://a": read("moneydj_sample.html"), "http://old": read("moneydj_old.html"),
         "http://short": read("moneydj_short.html")}
REF = date(2026, 10, 6)


def test_fetch_all(tmp_path):
    s = Sess(PAGES)
    res = eh.fetch_all("2026-10-06", tmp_path / "raw", SOURCES, s, ref_date=REF)
    assert all(r.encoding == "utf-8" for r in s.resps)
    assert sorted(res["top10"]) == ["00981A", "00988A"]
    assert res["data_dates"] == {"00981A": "2026-10-05", "00988A": "2026-10-02"}
    assert res["overseas"] == ["00989A"]
    assert set(res["missing"]) == {"00984A", "00982A"} and "只解析出" in res["missing"]["00984A"]
    assert res["stale"] == {"00988A": 4}  # 10/06 − 10/02 = 4 天 > 3
    assert (tmp_path / "raw" / "etf_00981A_2026-10-06.html").exists()


def test_stale_boundary():
    res = eh.fetch_all("2026-10-05", None, {"00988A": SOURCES["00988A"]}, Sess(PAGES), ref_date=date(2026, 10, 5))
    assert res["stale"] == {}  # 剛好 3 天不警告


def test_update_json_template_manual(tmp_path):
    res = eh.update("2026-10-06", tmp_path, sources=SOURCES, session=Sess(PAGES), ref_date=REF)
    saved = json.loads((tmp_path / "2026-10-06.json").read_text(encoding="utf-8"))
    assert saved["data_dates"]["00988A"] == "2026-10-02" and saved["stale"] == {"00988A": 4}
    assert saved["top10"]["00981A"] == EXPECTED and saved["overseas"] == ["00989A"]
    rows = list(csv.DictReader((tmp_path / "manual_template.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 20 and {r["etf_code"] for r in rows} == {"00984A", "00982A"}
    with open(tmp_path / "manual.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(eh.MANUAL_COLUMNS)
        for i, (t, wt) in enumerate(EXPECTED.items(), 1):
            w.writerow(["00984A", "", i, t, wt * 100])
    res = eh.update("2026-10-06", tmp_path, sources=SOURCES, session=Sess(PAGES), ref_date=REF)
    assert res["manual"] == ["00984A"] and set(res["missing"]) == {"00982A"}
    assert eh.load_top10(None, tmp_path)["00984A"] == pytest.approx(EXPECTED)
    assert eh.load_data_dates(None, tmp_path)["00984A"] == "manual"


def test_manual_validation(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("etf_code,name,rank,ticker,weight_pct\n00981A,,1,2330,9.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="只填了 1 檔"):
        eh.load_manual(p)
    p.write_text("etf_code,name,rank,ticker,weight_pct\nXXXX,,1,2330,9.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="未知的 ETF"):
        eh.load_manual(p)


# ---- Active Share 串接
ETF = {"00981A": EXPECTED}


def test_check_target_weights_pass_and_fail():
    far = {f"{3000 + i}": 0.06 for i in range(10)}
    r = eh.check_target_weights(far, ETF, expected_etfs=["00981A"])
    assert r["ok"] and r["safe"] and r["complete"] and not r["fails"]

    r = eh.check_target_weights(dict(EXPECTED), ETF, expected_etfs=["00981A"])
    assert not r["ok"] and r["fails"][0]["etf"] == "00981A"
    f = r["fails"][0]
    assert f["overlap_tickers"][0]["ticker"] == "2330"
    assert f["needed_cut"] == pytest.approx(0.2, abs=1e-3)


def test_check_target_weights_margin_and_missing():
    mine = {t: w for t, w in list(EXPECTED.items())[:8]}
    mine.update({"9001": 0.04, "9002": 0.03})
    r = eh.check_target_weights(mine, ETF, margin=0.0, expected_etfs=["00981A", "00980A"])
    assert r["missing"] == ["00980A"] and r["complete"] is False
    r2 = eh.check_target_weights(mine, ETF, margin=0.9, expected_etfs=["00981A"])
    assert r2["ok"] == r["ok"] and r2["safe"] is False


# ---- 真實 MoneyDJ 頁面（tests/fixtures/etf/real/，本機於 2026-10-06 存下）
REAL = FX / "real"


def real(code):
    return eh.parse_moneydj((REAL / f"moneydj_{code}.html").read_text(encoding="utf-8"), code)


def test_real_00981A_ignores_earlier_industry_chart_date():
    r = real("00981A")  # 頁面較前面有「持股依產業圖」的資料日期 2026/08/31，不可取到
    assert r["data_date"] == "2026-10-05"
    assert list(r["holdings"].items())[:3] == [("2330", 0.0983), ("3037", 0.0887), ("2383", 0.0882)]
    assert len(r["holdings"]) == 10 and r["holdings"]["8046"] == 0.0445


def test_real_00985A():
    r = real("00985A")
    assert r["data_date"] == "2026-10-05" and r["holdings"]["2330"] == 0.1601 and len(r["holdings"]) == 10


def test_real_00988A_global_keeps_non_tw_symbols():
    r = real("00988A")
    assert r["data_date"] == "2026-10-02"
    h = r["holdings"]
    assert h["LITE.US"] == 0.0747 and h["009150.KS"] == 0.0481 and h["6981.JP"] == 0.0426
    assert h["3037"] == 0.0626 and h["2454"] == 0.0347  # 持有的台股仍轉成 4 位代號
    assert sum(1 for t in h if t.isdigit() and len(t) == 4) == 2
