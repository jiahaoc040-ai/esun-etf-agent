"""資料包（DataPack 規格書 v4.3）：解析、驗證、尋找、對帳、摘要。範例取自規格書 §3。"""
import copy
import json
import os

import pytest

from esun_agent import datapack as dpk

EXEC = {
    "schema_version": "3.0", "doc_type": "DataPack", "team_id": "TEAM_042", "trade_date": "2026-10-28",
    "as_of_date": "2026-10-27", "generated_at": "2026-10-27T18:58:18+08:00", "missing": [],
    "prev_day_orders": {"outcome": "executed", "void_reasons": [], "excluded_codes": []},
    "account": {"cash": 135237741.0, "cash_ratio_tradable": 0.135265, "nav_tradable": 999795141.0, "aum": 1000795141.0,
                "receivable_cash": 1000000.0, "holdings_count": 3},
    "holdings": [
        {"code": "2330", "shares": 73000, "receivable_shares": 0, "close_price": 2585.0, "tradable_weight": 0.188744},
        {"code": "2317", "shares": 267000, "receivable_shares": 0, "close_price": 256.0, "tradable_weight": 0.068366},
        {"code": "6669", "shares": 3000, "receivable_shares": 9913, "close_price": 2610.0, "tradable_weight": 0.00783},
        {"code": "2454", "shares": 0, "receivable_shares": 500, "close_price": 4920.0, "tradable_weight": None}],
    "compliance": {"violations": 0, "notices": 0, "incidents": []},
}
# 讓 nav_tradable = 1e9 = cash + Σ shares × close_price
EXEC["account"]["nav_tradable"] = 1e9
EXEC["account"]["cash"] = 1e9 - (73000 * 2585.0 + 267000 * 256.0 + 3000 * 2610.0)
FIRST = {"schema_version": "3.0", "doc_type": "DataPack", "team_id": "TEAM_042", "trade_date": "2026-10-27",
         "as_of_date": None, "generated_at": "2026-10-27T05:12:08+08:00", "missing": [],
         "prev_day_orders": {"outcome": "none", "void_reasons": [], "excluded_codes": []},
         "account": {"cash": 1e9, "cash_ratio_tradable": 1.0, "nav_tradable": 1e9, "aum": 1e9, "receivable_cash": 0.0,
                     "holdings_count": 0},
         "holdings": [], "compliance": {"violations": 0, "notices": 0, "incidents": []}}
NOT_SETTLED = {"schema_version": "3.0", "doc_type": "DataPack", "team_id": "TEAM_042", "trade_date": "2026-10-29",
               "as_of_date": None, "generated_at": "2026-10-28T18:58:11+08:00", "missing": ["prev_settlement"],
               "prev_day_orders": {"outcome": "unknown", "void_reasons": [], "excluded_codes": []},
               "account": None, "holdings": None, "compliance": {"violations": 0, "notices": 0, "incidents": []}}


def test_parse_uses_nav_tradable_shares_and_cash_not_aum_or_receivables():
    dp = dpk.parse_datapack(EXEC, "2026-10-28", "TEAM_042")
    assert dp.nav == EXEC["account"]["nav_tradable"] and dp.nav != EXEC["account"]["aum"]
    assert dp.cash == EXEC["account"]["cash"]
    assert dp.holdings == {"2330": 73000, "2317": 267000, "6669": 3000}          # receivable 不計、shares=0 不算持股
    assert dp.receivable == {"6669": 9913, "2454": 500} and "2454" not in dp.weights
    assert dp.warnings == [] and not dp.first_day


def test_first_trading_day_is_true_all_cash():
    dp = dpk.parse_datapack(FIRST, "2026-10-27")
    assert dp.first_day and dp.holdings == {} and dp.cash == dp.nav == 1e9
    bad = copy.deepcopy(FIRST)
    bad["holdings"] = EXEC["holdings"][:1]
    with pytest.raises(dpk.DataPackError, match="第一個交易日"):
        dpk.parse_datapack(bad, "2026-10-27")


@pytest.mark.parametrize("patch", [{}, {"missing": []}, {"missing": [], "account": EXEC["account"]},
                                   {"missing": ["prev_settlement"], "account": EXEC["account"], "holdings": []}])
def test_not_settled_is_never_treated_as_empty(patch):
    d = {**NOT_SETTLED, **patch}
    with pytest.raises(dpk.NotSettledError, match="結算尚未產生，請稍後重新下載"):
        dpk.parse_datapack(d, "2026-10-29")


@pytest.mark.parametrize("field,value,match", [("doc_type", "D-Plan", "doc_type"), ("trade_date", "2026-10-29", "trade_date"),
                                               ("team_id", "TEAM_043", "team_id")])
def test_identity_checks(field, value, match):
    d = {**EXEC, field: value}
    with pytest.raises(dpk.DataPackError, match=match):
        dpk.parse_datapack(d, "2026-10-28", "TEAM_042")


def test_suspicious_values_become_warnings():
    d = copy.deepcopy(EXEC)
    d["account"]["nav_tradable"] += 5_000
    d["account"]["holdings_count"] = 4
    d["missing"] = ["prev_close"]
    d["holdings"][1]["shares"] = 267500
    d["holdings"][1]["close_price"] = None
    dp = dpk.parse_datapack(d, "2026-10-28")
    w = " ".join(dp.warnings)
    assert "nav_tradable" in w and "holdings_count" in w and "prev_close" in w and "1000 的倍數" in w and "無收盤價" in w


def test_find_latest_matching_datapack(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "old.json").write_text(json.dumps({**EXEC, "generated_at": "2026-10-27T18:00:00+08:00"}))
    (b / "new.json").write_text(json.dumps(EXEC))
    (b / "other_day.json").write_text(json.dumps({**EXEC, "trade_date": "2026-10-29", "generated_at": "2026-10-28T19:00:00+08:00"}))
    (b / "dplan.json").write_text(json.dumps({"doc_type": "D-Plan", "trade_date": "2026-10-28"}))
    (b / "broken.json").write_text("{not json")
    (b / "notes.txt").write_text("x")
    assert dpk.find_datapack("2026-10-28", [a, b, tmp_path / "missing"]) == b / "new.json"
    assert dpk.find_datapack("2026-10-30", [a, b]) is None
    # generated_at 相同 → 修改時間較新者
    (a / "same.json").write_text(json.dumps(EXEC))
    os.utime(a / "same.json", (2e9, 2e9))
    assert dpk.find_datapack("2026-10-28", [a, b]) == a / "same.json"


def test_default_search_dirs_are_isolated_in_tests():
    assert "isolated" in str(dpk.DOWNLOADS_DIR) and "isolated" in str(dpk.DATAPACK_DIR)
    assert dpk.find_datapack("2026-10-28") is None


def test_reconcile_lists_every_difference():
    dp = dpk.parse_datapack(EXEC)
    diffs = dpk.reconcile({"2330": 74000, "2317": 267000, "2412": 1000}, dp.cash + 100, dp)
    assert any(d.startswith("2330: 自記 74,000") for d in diffs) and any(d.startswith("2412") for d in diffs)
    assert any(d.startswith("6669") for d in diffs) and any(d.startswith("現金") for d in diffs)
    assert dpk.reconcile(dict(dp.holdings), dp.cash, dp) == []


def test_summary_voided_excluded_and_incidents():
    d = copy.deepcopy(EXEC)
    d["prev_day_orders"] = {"outcome": "voided", "void_reasons": ["oversell", "dplan_round_lot"], "excluded_codes": ["2454"]}
    d["compliance"] = {"violations": 1, "notices": 2, "incidents": [
        {"date": "2026-10-27", "check_code": "oversell", "status": "fail", "message": "賣出 5,000 股超過庫存 3,000 股"},
        {"date": "2026-10-27", "check_code": "single_weight", "status": "warn", "message": "2330 權重 25.3% 超過上限（連續第 1 日）"},
        {"date": "2026-10-20", "check_code": "single_weight", "status": "warn", "message": "2317 權重 10.2% 超過上限"},
        {"date": "2026-10-27", "check_code": "x", "status": "pass", "message": "ok"}]}
    dp = dpk.parse_datapack(d)
    s = "\n".join(dpk.summary_lines(dp))
    assert "voided" in s and "oversell（賣出股數超過持股）" in s and "dplan_round_lot" in s and "被排除未成交：2454" in s
    al = dpk.alert_lines(dp)
    assert len(al) == 3 and "違規 FAIL" in al[0] and "提示 WARN" in al[1]
    assert dp.weight_incident_tickers(since="2026-10-27") == (True, {"2330"})      # 只看最新結算日
    assert dp.weight_incident_tickers() == (True, {"2330", "2317"})


def test_overweight_uses_nav_tradable_and_prev_close():
    dp = dpk.parse_datapack(EXEC)
    w = dpk.overweight(dp, {"2330": 2585.0 * 1.3, "2317": 256.0 * 1.3})          # 24.5%（> 24%）、8.9%（≤ 9.5%）
    assert set(w) == {"2330"} and w["2330"] == pytest.approx(73000 * 2585.0 * 1.3 / dp.nav)
    assert dpk.overweight(dp, {"2317": 256.0 * 6}) == {"2317": pytest.approx(267000 * 256.0 * 6 / dp.nav)}


def test_unreadable_folder_is_reported(tmp_path, monkeypatch):
    from pathlib import Path
    real_glob = Path.glob

    def deny(self, pat):
        if self.name == "Downloads":
            raise PermissionError("Operation not permitted")
        return real_glob(self, pat)
    (tmp_path / "Downloads").mkdir()
    monkeypatch.setattr(Path, "glob", deny)
    errs = []
    assert dpk.find_datapack("2026-10-28", [tmp_path / "Downloads"], errs) is None
    assert errs and "完整磁碟取用權限" in errs[0]
