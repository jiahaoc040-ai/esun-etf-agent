import csv
import json
from pathlib import Path

import pytest

from esun_agent.data import etf_holdings as eh

FX = Path(__file__).parent / "fixtures" / "etf"
EXPECTED = {"2330": 0.095, "2454": 0.081, "2317": 0.062, "2308": 0.055, "2383": 0.05,
            "3017": 0.044, "2382": 0.041, "3231": 0.038, "2345": 0.033, "3443": 0.03}


def read(name):
    return (FX / name).read_text(encoding="utf-8-sig")


def test_parse_ticker_and_weight():
    assert eh.parse_ticker("2330 台積電") == "2330" and eh.parse_ticker("2330.TW") == "2330"
    assert eh.parse_ticker("00981A") == "00981A" and eh.parse_ticker("現金") is None
    assert eh.parse_weight("8.52%") == (8.52, True) and eh.parse_weight("1,234") == (1234.0, False)
    assert eh.parse_weight("--") is None


def test_parse_html_json_csv_agree():
    assert eh.parse_html(read("issuer_a.html"), "a") == EXPECTED   # 略過第一個無關表格、「現金」列、第 11 名
    assert eh.parse_json(json.loads(read("issuer_b.json")), "b") == EXPECTED
    assert eh.parse_csv_text(read("issuer_c.csv"), "c") == EXPECTED  # 無代號欄 → 用名稱反查 150 檔


def test_fraction_weights_not_rescaled():
    rows = [{"代號": str(2000 + i), "權重": 0.05} for i in range(12)]
    assert set(eh.normalize_rows(rows).values()) == {0.05}


def test_parse_failures_raise():
    with pytest.raises(ValueError, match="找不到持股表格"):
        eh.parse_html(read("issuer_bad.html"), "bad")
    with pytest.raises(ValueError, match="只解析出"):
        eh.normalize_rows([{"代號": "2330", "權重": "5%"}] * 3, "few")
    with pytest.raises(ValueError, match="找不到代號"):
        eh.normalize_rows([{"foo": 1, "bar": 2}], "x")
    with pytest.raises(ValueError):
        eh.parse_json({"a": 1}, "j")


def test_sources_file_covers_30_etfs_and_flags_overseas():
    src = eh.load_sources()
    assert len(src) == 30
    assert {c for c, s in src.items() if s["overseas"]} == {"00983A", "00989A", "00988A", "00997A",
                                                           "00402A", "00409A"}


class Resp:
    def __init__(self, text): self.text, self.encoding = text, "utf-8"
    def raise_for_status(self): pass


class Sess:
    headers = {}
    def __init__(self, pages): self.pages = pages
    def get(self, url, timeout=None):
        if url not in self.pages:
            raise ConnectionError("blocked")
        return Resp(self.pages[url])


SOURCES = {
    "00981A": {"issuer": "a", "overseas": False, "format": "html", "url": "http://a"},
    "00980A": {"issuer": "b", "overseas": False, "format": "json", "url": "http://b"},
    "00985A": {"issuer": "c", "overseas": False, "format": "csv", "url": "http://c"},
    "00984A": {"issuer": "d", "overseas": False, "format": "html", "url": "http://bad"},
    "00982A": {"issuer": "e", "overseas": False, "format": "", "url": ""},
    "00989A": {"issuer": "f", "overseas": True, "format": "", "url": ""},
}
PAGES = {"http://a": read("issuer_a.html"), "http://b": read("issuer_b.json"),
         "http://c": read("issuer_c.csv"), "http://bad": read("issuer_bad.html")}


def test_fetch_all_isolates_failures_and_saves_raw(tmp_path):
    res = eh.fetch_all("2026-10-06", tmp_path / "raw", SOURCES, Sess(PAGES))
    assert sorted(res["top10"]) == ["00980A", "00981A", "00985A"]
    assert res["overseas"] == ["00989A"]
    assert set(res["missing"]) == {"00984A", "00982A"}
    assert "找不到持股表格" in res["missing"]["00984A"] and "尚未填" in res["missing"]["00982A"]
    assert (tmp_path / "raw" / "etf_00981A_2026-10-06.html").exists()


def test_update_writes_json_template_and_merges_manual(tmp_path):
    res = eh.update("2026-10-06", tmp_path, sources=SOURCES, session=Sess(PAGES))
    saved = json.loads((tmp_path / "2026-10-06.json").read_text(encoding="utf-8"))
    assert sorted(saved) == ["00980A", "00981A", "00985A"] and saved["00981A"] == EXPECTED
    tpl = tmp_path / "manual_template.csv"
    rows = list(csv.DictReader(tpl.open(encoding="utf-8-sig")))
    assert len(rows) == 20 and {r["etf_code"] for r in rows} == {"00984A", "00982A"}
    # 使用者填 00984A 後存成 manual.csv，再跑一次就不再缺
    with open(tmp_path / "manual.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(eh.MANUAL_COLUMNS)
        for i, (t, wt) in enumerate(EXPECTED.items(), 1):
            w.writerow(["00984A", "", i, t, wt * 100])
    res = eh.update("2026-10-06", tmp_path, sources=SOURCES, session=Sess(PAGES))
    assert res["manual"] == ["00984A"] and set(res["missing"]) == {"00982A"}
    assert eh.load_top10(None, tmp_path)["00984A"] == pytest.approx(EXPECTED)


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

    copy = dict(EXPECTED)
    r = eh.check_target_weights(copy, ETF, expected_etfs=["00981A"])
    assert not r["ok"] and r["fails"][0]["etf"] == "00981A"
    f = r["fails"][0]
    assert f["overlap_tickers"][0]["ticker"] == "2330"  # 重疊最大者排最前
    assert f["needed_cut"] == pytest.approx(0.2, abs=1e-3)


def test_check_target_weights_margin_and_missing():
    mine = {t: w for t, w in list(EXPECTED.items())[:8]}
    mine.update({"9001": 0.04, "9002": 0.03})
    r = eh.check_target_weights(mine, ETF, margin=0.0, expected_etfs=["00981A", "00980A"])
    assert r["missing"] == ["00980A"] and r["complete"] is False
    r2 = eh.check_target_weights(mine, ETF, margin=0.9, expected_etfs=["00981A"])
    assert r2["ok"] == r["ok"] and r2["safe"] is False
