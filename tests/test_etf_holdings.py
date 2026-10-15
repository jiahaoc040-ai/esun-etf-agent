import csv
import json
from datetime import date
from pathlib import Path

import pytest

from esun_agent.data import etf_holdings as eh

FX = Path(__file__).parent / "fixtures" / "etf"
# 舊格式（Basic0007，只有前 10 大）fixture 的期望值；解析時需 n=10
EXPECTED = {"2330": 0.095, "2327": 0.081, "LITE.US": 0.062, "009150.KS": 0.055, "2454": 0.05,
            "2317": 0.044, "2308": 0.041, "2382": 0.038, "2345": 0.033, "3443": 0.03}


# 新格式（Basic0007B 全部持股）fixture 的期望前 15 大（現金／期貨／應收等列已排除，第 16 名以後被截掉）
EXPECTED15 = {"2330": 0.095, "2327": 0.081, "LITE.US": 0.062, "009150.KS": 0.055, "2454": 0.05, "2317": 0.044,
              "2308": 0.041, "2382": 0.038, "2345": 0.033, "3443": 0.03, "3037": 0.029, "3017": 0.028,
              "3653": 0.027, "6223": 0.026, "8046": 0.025}


def read(name):
    return (FX / name).read_text(encoding="utf-8")


def test_parse_holding_name():
    p = eh.parse_holding_name
    assert p("台積電(2330.TW)") == "2330" and p("國巨*(2327.TW)") == "2327"
    assert p("Lumentum(LITE.US)") == "LITE.US" and p("Samsung Elec Mech(009150.KS)") == "009150.KS"
    assert p("X(00679B.TW)") == "00679B.TW"  # .TW 但非 4 位數 → 保留原字串
    assert p("英飛凌科技股份有限公司") == "英飛凌科技股份有限公司"  # 無括號代號 → 完整名稱
    assert p("") is None and p(None) is None


def test_parse_moneydj_all_holdings_top15():
    r = eh.parse_moneydj(read("moneydj_all_holdings.html"), "00981A")
    # 取全部持股表的 10/05，而不是頁面上更早出現的另一個「資料日期」(10/01)
    assert r["data_date"] == "2026-10-05"
    assert r["holdings"] == EXPECTED15 and len(r["holdings"]) == 15
    # 現金、台指期貨（FITXN*1.TF）、期貨保證金、應收付息不算持股；第 16 名以後（緯穎、世芯）被截掉
    assert not {"6669", "3661"} & set(r["holdings"]) and not any(".TF" in t or "FITXN" in t for t in r["holdings"])
    # raw 保留表格全部原始列（22 列，含現金／期貨）
    assert len(r["raw"]) == 22
    assert ["台指期貨 202610(FITXN*1.TF)", 3.2] in r["raw"] and ["現金", 6.0] in r["raw"]


def test_futures_row_excluded_by_code_even_without_keyword():
    html = read("moneydj_all_holdings.html").replace("台指期貨 202610(FITXN*1.TF)", "FITXN 近月合約(FITXN*1.TF)")
    r = eh.parse_moneydj(html, "x")
    assert r["holdings"] == EXPECTED15
    assert eh.is_non_holding("FITXN 近月合約(FITXN*1.TF)") and not eh.is_non_holding("台積電(2330.TW)")


def test_parse_moneydj_failures():
    with pytest.raises(ValueError, match="只解析出 4 檔"):
        eh.parse_moneydj(read("moneydj_short.html"), "x", n=10)
    with pytest.raises(ValueError, match="沒有「全部持股」或「持股明細」"):
        eh.parse_moneydj(read("moneydj_nodetail.html"), "x")
    # 舊格式（只有前 10 大）頁面不夠 15 檔 → 明確報錯，不輸出殘缺資料
    with pytest.raises(ValueError, match="只解析出 10 檔持股（需要至少 15）"):
        eh.parse_moneydj(read("moneydj_sample.html"), "x")


def test_legacy_top10_format_still_parses_with_n10():
    """舊格式（Basic0007，前 10 大）fixture：保留作回歸，需傳 n=10。"""
    r = eh.parse_moneydj(read("moneydj_sample.html"), "00981A", n=10)
    assert r["data_date"] == "2026-10-05" and r["holdings"] == EXPECTED


def test_sources_file():
    src = eh.load_sources()
    assert len(src) == 30
    assert {c for c, s in src.items() if s["overseas"]} == {"00989A", "00997A", "00402A"}
    # 全球型但持有台股者不可跳過
    assert not any(src[c]["overseas"] for c in ("00983A", "00988A", "00409A"))
    assert src["00981A"]["url"] == "https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid=00981A.TW"
    assert eh.MONEYDJ_URL.endswith("Basic0007B.xdjhtm?etfid={code}.TW")


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
PAGES = {"http://a": read("moneydj_all_holdings.html"),
         "http://old": read("moneydj_all_holdings.html").replace("2026/10/05", "2026/10/02"),
         "http://short": read("moneydj_short.html")}
REF = date(2026, 10, 6)


def test_fetch_all(tmp_path):
    s = Sess(PAGES)
    res = eh.fetch_all("2026-10-06", tmp_path / "raw", SOURCES, s, ref_date=REF)
    assert all(r.encoding == "utf-8" for r in s.resps)
    assert sorted(res["top15"]) == ["00981A", "00988A"] and sorted(res["raw"]) == ["00981A", "00988A"]
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
    assert saved["top15"]["00981A"] == EXPECTED15 and saved["overseas"] == ["00989A"]
    assert len(saved["raw"]["00981A"]) == 22          # 全部持股原始列（含現金／期貨）仍保留
    rows = list(csv.DictReader((tmp_path / "manual_template.csv").open(encoding="utf-8-sig")))
    assert len(rows) == 30 and {r["etf_code"] for r in rows} == {"00984A", "00982A"}
    with open(tmp_path / "manual.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(eh.MANUAL_COLUMNS)
        for i, (t, wt) in enumerate(EXPECTED15.items(), 1):
            w.writerow(["00984A", "", i, t, wt * 100])
    res = eh.update("2026-10-06", tmp_path, sources=SOURCES, session=Sess(PAGES), ref_date=REF)
    assert res["manual"] == ["00984A"] and set(res["missing"]) == {"00982A"}
    assert eh.load_top15(None, tmp_path)["00984A"] == pytest.approx(EXPECTED15)
    assert eh.load_data_dates(None, tmp_path)["00984A"] == "manual"


def test_manual_validation(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("etf_code,name,rank,ticker,weight_pct\n00981A,,1,2330,9.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="只填了 1 檔，需要 15 檔"):
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
    assert f["needed_cut"] == pytest.approx(0.15, abs=1e-3)   # 紅線 15%


def test_check_target_weights_margin_and_missing():
    mine = {t: w for t, w in list(EXPECTED.items())[:8]}
    mine.update({"9001": 0.04, "9002": 0.03})
    r = eh.check_target_weights(mine, ETF, margin=0.0, expected_etfs=["00981A", "00980A"])
    assert r["missing"] == ["00980A"] and r["complete"] is False
    r2 = eh.check_target_weights(mine, ETF, margin=0.9, expected_etfs=["00981A"])
    assert r2["ok"] == r["ok"] and r2["safe"] is False


# ---- 真實 MoneyDJ 頁面：**舊格式**（Basic0007 前 10 大；tests/fixtures/etf/real/moneydj_<code>.html，2026-10-06 存下），解析時 n=10
REAL = FX / "real"


def real(code):
    return eh.parse_moneydj((REAL / f"moneydj_{code}.html").read_text(encoding="utf-8"), code, n=10)


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


def test_row_without_bracket_code_is_kept_as_name():
    # 00986A 情境：第 10 列「英飛凌科技股份有限公司|3.15|8,000.00」沒有括號代號
    html = read("moneydj_sample.html")
    body = "".join(f'<tr><td><a href="#">{n}</a></td><td>{w:.2f}</td><td>1,000</td></tr>'
                   for n, w in [("台積電(2330.TW)", 9.5), ("國巨*(2327.TW)", 8.1), ("聯發科(2454.TW)", 5.0),
                                ("鴻海(2317.TW)", 4.4), ("台達電(2308.TW)", 4.1), ("廣達(2382.TW)", 3.8),
                                ("智邦(2345.TW)", 3.3), ("創意(3443.TW)", 3.0), ("欣興(3037.TW)", 2.5),
                                ("英飛凌科技股份有限公司", 3.15)])
    start, end = html.index("<tbody>") + 7, html.index("</tbody>")
    r = eh.parse_moneydj(html[:start] + body + html[end:], "00986A", n=10)      # 舊格式
    assert len(r["holdings"]) == 10
    assert r["holdings"]["英飛凌科技股份有限公司"] == 0.0315
    assert r["holdings"]["2330"] == 0.095


def test_non_holding_rows_excluded():
    html = read("moneydj_sample.html")
    extra = "".join(f"<tr><td>{n}</td><td>{w}</td><td>1</td></tr>" for n, w in [
        ("現金", 20.0), ("台指期貨(TXF)", 15.0), ("期貨保證金", 5.0), ("應收付息", 4.0),
        ("應付款項", 3.0), ("附買回債券", 2.5), ("美元現金(USD)", 6.0)])
    html = html.replace("<tbody>", "<tbody>" + extra)
    r = eh.parse_moneydj(html, "x", n=10)      # 舊格式
    assert r["holdings"] == EXPECTED


def test_check_target_weights_uses_top15_and_15pct_floor():
    from esun_agent.active_share import top_n
    from esun_agent.config import ACTIVE_SHARE_MIN, ACTIVE_SHARE_TOP_N
    assert (ACTIVE_SHARE_TOP_N, ACTIVE_SHARE_MIN) == (15, 0.15)
    big = {f"{3000 + i}": 0.05 - i * 0.001 for i in range(20)}
    assert len(top_n(big)) == 15
    # 我方與 ETF 的前 15 大完全相同 → 0%；只有第 16 名以後不同不影響結果
    r = eh.check_target_weights(dict(big), {"E": dict(big)}, expected_etfs=["E"])
    assert not r["ok"] and r["min"] == pytest.approx(0, abs=1e-9)
    # 10 檔相同、5 檔不同（AS = 17%）→ 在新紅線 15% 之上（通過），舊紅線 20% 之下
    mine = dict(list(big.items())[:10]) | {f"90{j}": 0.03 for j in range(5)}
    r = eh.check_target_weights(mine, {"E": dict(big)}, margin=0.0, expected_etfs=["E"])
    assert r["min"] == pytest.approx(0.17, abs=1e-3) and r["ok"]
    # 11 檔相同 → 13.5% < 15% → 違規
    mine = dict(list(big.items())[:11]) | {f"90{j}": 0.03 for j in range(4)}
    assert not eh.check_target_weights(mine, {"E": dict(big)}, margin=0.0, expected_etfs=["E"])["ok"]


# ---- 真實 MoneyDJ 全部持股頁（Basic0007B；tests/fixtures/etf/real/moneydj_all_*.html，2026-10-11 存下）
def real_b(code):
    return eh.parse_moneydj((REAL / f"moneydj_all_{code}.html").read_text(encoding="utf-8"), code)


def test_real_b_00981A_top15_and_futures_row_excluded():
    r = real_b("00981A")
    assert r["data_date"] == "2026-10-08" and len(r["holdings"]) == 15 and len(r["raw"]) == 51
    assert list(r["holdings"].items())[:3] == [("2330", 0.1035), ("2383", 0.0958), ("2454", 0.0819)]
    assert ["台指期貨 2026/10(FITXN*1.TF)", 2.3] in r["raw"]          # 原始列保留
    assert not any(".TF" in t or "FITXN" in t for t in r["holdings"])  # 但不算進前 15 大
    assert sum(r["holdings"].values()) < 1.0


def test_real_b_00985A_futures_named_by_company():
    r = real_b("00985A")   # 「台積電期貨(FICDFN*1.TF)」不是 2330 股票，不可併入台積電持股
    assert r["data_date"] == "2026-10-08" and r["holdings"]["2330"] == 0.1332 and len(r["holdings"]) == 15
    assert ["台積電期貨(FICDFN*1.TF)", 2.77] in r["raw"] and not any("FICDFN" in t for t in r["holdings"])


def test_real_b_global_etfs_keep_non_tw_symbols():
    r = real_b("00986A")
    assert r["data_date"] == "2026-10-07" and r["holdings"]["NVDA.US"] == 0.064 and r["holdings"]["2330"] == 0.0789
    r = real_b("00988A")
    assert r["data_date"] == "2026-10-07" and r["holdings"]["LITE.US"] == 0.0773 and r["holdings"]["3037"] == 0.0634
    assert len(r["holdings"]) == 15


def test_committed_top15_snapshot_matches_real_pages_and_is_complete(tmp_path):
    snap_path = Path(__file__).parent.parent / "data" / "etf_top15" / "2026-10-11.json"      # 指定日期的 commit 快照
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    src = eh.load_sources()
    expected = {c for c, s in src.items() if not s["overseas"]}
    assert set(snap["top15"]) == expected and len(expected) == 27 and snap["stale"] == {}
    assert all(len(h) == 15 and 0.2 < sum(h.values()) < 1.0 for h in snap["top15"].values())
    assert set(snap["raw"]) == expected
    for code in ("00981A", "00985A", "00986A", "00988A"):                 # 快照與存下的原始頁面一致
        r = real_b(code)
        assert snap["top15"][code] == r["holdings"] and snap["raw"][code] == r["raw"]
        assert snap["data_dates"][code] == r["data_date"]
    # 預設讀取最新一份：在暫存資料夾放一份較舊的檔，確認取到日期最新者（不讀本機 data/etf_top15 的最新檔）
    older = {**snap, "top15": {"00981A": {"2330": 0.1}}}
    (tmp_path / "2026-10-01.json").write_text(json.dumps(older), encoding="utf-8")
    (tmp_path / "2026-10-11.json").write_text(snap_path.read_text(encoding="utf-8"), encoding="utf-8")
    assert eh.load_top15(base=tmp_path) == snap["top15"]


def test_active_share_vs_real_etfs_of_cap_weighted_core():
    """真實 ETF 前 15 大 vs 一個市值加權風格的前 15 大：確認整條串接可算，且 2330 單押時 Active Share 很高。"""
    top = eh.load_top15()
    only_tsmc_heavy = {"2330": 0.25, "2317": 0.1, "2454": 0.1, "2308": 0.05, "2382": 0.05, "2881": 0.05, "2882": 0.05,
                       "2891": 0.04, "2412": 0.04, "1301": 0.03, "1303": 0.03, "2002": 0.03, "1216": 0.03, "2886": 0.03, "2884": 0.03}
    r = eh.check_target_weights(only_tsmc_heavy, top, margin=0.0, expected_etfs=list(top))
    assert r["complete"] and r["ok"] and r["min"] > 0.15
