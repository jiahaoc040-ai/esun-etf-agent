import copy
import json
from pathlib import Path

import pytest

from esun_agent.dplan_validate import validate

ROOT = Path(__file__).resolve().parent.parent
COMP = ROOT / "docs/competition"
CTX = json.loads((ROOT / "tests/fixtures/ctx_example_2026-10-27.json").read_text(encoding="utf-8"))
POS = json.loads((COMP / "D-Plan_TEAM_042_2026-10-27.json").read_text(encoding="utf-8"))
NEG = json.loads((COMP / "D-Plan_TEAM_043_2026-10-27.json").read_text(encoding="utf-8"))


def codes(rep):
    return sorted({e.split("]")[0].strip("[") for e in rep.errors})


def test_positive_example_only_fails_holding_count():
    # 正例為節錄版（只列 3–4 檔），所以只會違反 20–30 檔限制
    rep = validate(POS, CTX, filename="D-Plan_TEAM_042_2026-10-27.json")
    assert codes(rep) == ["C11"], rep


def test_negative_example_flags_official_violations():
    rep = validate(NEG, CTX, filename="D-Plan_TEAM_043_2026-10-27.json")
    c = codes(rep)
    assert "C1" in c and "C2" in c and "C12" in c, rep
    assert any("I9" in e for e in rep.errors)


def test_schema_and_chain_without_ctx():
    assert validate(POS).ok


def test_filename_mismatch():
    rep = validate(POS, filename="D-Plan_TEAM_042_2026-10-28.json")
    assert "FILENAME" in codes(rep)


def test_id_must_be_contiguous():
    doc = copy.deepcopy(POS)
    doc["sources"][1]["source_id"] = "S02"
    assert "SCHEMA" in codes(validate(doc)) or "ID" in codes(validate(doc))


def test_oversell_rejected():
    ctx = copy.deepcopy(CTX)
    ctx["holdings"]["2330"] = 1000
    doc = copy.deepcopy(POS)
    doc["orders"][0]["shares"] = 2000
    assert "C13" in codes(validate(doc, ctx))


def test_uncovered_holding():
    ctx = copy.deepcopy(CTX)
    ctx["holdings"]["2454"] = 3000
    assert "COVER" in codes(validate(POS, ctx))


def test_funding_for_without_gap_rejected():
    ctx = copy.deepcopy(CTX)
    ctx["cash"] = 9_000_000          # 錢夠買 2891，就不該宣稱調度
    ctx["prev_nav"] = 102_000_000
    doc = copy.deepcopy(POS)
    doc["market_view"]["posture"]["target_cash_pct_range"] = [0.03, 0.10]
    assert "C14" in codes(validate(doc, ctx))


def test_cash_ratio_cap_is_inclusive(monkeypatch):
    """1005 版辦法：現金「不超過」25% → 剛好等於上限不算違規（≤），超過才報 C9。"""
    from esun_agent import dplan_validate as dv
    buy, sell = dv.estimate_flows(POS["orders"], CTX["prev_close"])
    cash_pct = (CTX["cash"] - (buy - sell)) / CTX["prev_nav"]
    assert 0 < cash_pct < 0.25
    monkeypatch.setattr(dv, "CASH_MAX", cash_pct)                  # 剛好等於上限
    assert not any("現金比" in e and "C9" in e for e in validate(POS, CTX).errors)
    monkeypatch.setattr(dv, "CASH_MAX", cash_pct - 1e-6)          # 略低於 → 超過
    assert any("C9" in e and "現金比" in e and "> 25%" in e for e in validate(POS, CTX).errors)


# ------------------------------------------------------------------ schema v4.3 與官方 v4.3 正/反例
V43 = COMP / "v4.3"
CTX43 = json.loads((ROOT / "tests/fixtures/ctx_example_2026-10-28.json").read_text(encoding="utf-8"))
POS43 = json.loads((V43 / "D-Plan_TEAM_042_2026-10-28.json").read_text(encoding="utf-8"))
NEG43 = json.loads((V43 / "D-Plan_TEAM_043_2026-10-28.json").read_text(encoding="utf-8"))


def test_schema_is_v43_and_output_version():
    from esun_agent.config import SCHEMA_PATH, SCHEMA_VERSION
    assert SCHEMA_VERSION == "4.3" and SCHEMA_PATH.name == "D-Plan.schema_v4.3.json"
    assert json.loads(SCHEMA_PATH.read_text(encoding="utf-8")) == json.loads(
        (V43 / "D-Plan.schema_v4.3.json").read_text(encoding="utf-8"))           # 與官方檔一致


def test_v43_positive_example_zero_errors():
    rep = validate(POS43, CTX43, filename="D-Plan_TEAM_042_2026-10-28.json")
    assert rep.errors == [], rep
    assert validate(POS43, filename="D-Plan_TEAM_042_2026-10-28.json").errors == []


def test_v43_negative_example_three_errors():
    rep = validate(NEG43, CTX43, filename="D-Plan_TEAM_043_2026-10-28.json")
    assert set(codes(rep)) == {"C1", "C2", "C12"}, rep
    assert any("I9" in e for e in rep.errors)                                     # 孤兒決策
    assert any("D1" in e and "SELL', 3000" in e and "SELL', 2000" in e for e in rep.errors)   # 2330 委託 ≠ 推導
    assert any("宣告 reduce" in e and "需淨賣超" in e for e in rep.errors)          # 姿態與委託方向相反（實為淨買超）


def test_v43_orders_derived_from_nav_tradable_and_exchange_close():
    """撰寫指南 §⑥：目標股數 = floor(target_weight × nav_tradable ÷ T-1 交易所收盤 ÷ 1000) × 1000。"""
    from esun_agent.orders import derive_orders
    got = [o.to_dict() for o in derive_orders(POS43["decisions"], CTX43["prev_nav"], CTX43["prev_close"], CTX43["holdings"])]
    key = lambda o: (o["ticker"], o["side"], o["shares"], o["decision_ref"])
    assert sorted(map(key, got)) == sorted(map(key, POS43["orders"]))


def test_schema_v43_allows_up_to_60_decisions_and_4x_versions():
    doc = copy.deepcopy(POS43)
    doc["schema_version"] = "4.2"
    assert validate(doc).errors == []
    doc["schema_version"] = "5.0"
    assert "SCHEMA" in codes(validate(doc))


def test_holding_count_is_error_without_excerpt_flag():
    ctx = {k: v for k, v in CTX43.items() if k != "holdings_excerpt"}
    assert codes(validate(POS43, ctx)) == ["C11"]


def test_buy_action_requires_zero_settled_shares():
    ctx = copy.deepcopy(CTX43)
    ctx["holdings"]["2891"] = 3000                                                # 前日結算已有 → 應為 ADD
    assert any("應為 ADD" in e for e in validate(POS43, ctx).errors)


def test_limit_up_stress_test_on_buys():
    ctx = copy.deepcopy(CTX43)
    doc = copy.deepcopy(POS43)
    doc["decisions"][1]["target_weight"] = 0.095                                   # 9.5% × 1.10 = 10.45% > 10%
    from esun_agent.orders import derive_orders
    doc["orders"] = [o.to_dict() for o in derive_orders(doc["decisions"], ctx["prev_nav"], ctx["prev_close"], ctx["holdings"])]
    errs = validate(doc, ctx).errors
    assert any("漲停" in e and "2891" in e for e in errs)
    doc["decisions"][1]["target_weight"] = 0.09                                    # 9% × 1.10 = 9.9% ≤ 10%
    doc["orders"] = [o.to_dict() for o in derive_orders(doc["decisions"], ctx["prev_nav"], ctx["prev_close"], ctx["holdings"])]
    assert not any("漲停" in e for e in validate(doc, ctx).errors)


def test_validate_cli_with_ctx_and_datapack(tmp_path, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("vd", ROOT / "scripts" / "validate_dplan.py")
    vd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vd)
    pos = str(V43 / "D-Plan_TEAM_042_2026-10-28.json")
    assert vd.main([pos, "--ctx", str(ROOT / "tests/fixtures/ctx_example_2026-10-28.json")]) == 0
    assert vd.main([str(V43 / "D-Plan_TEAM_043_2026-10-28.json"), "--ctx",
                    str(ROOT / "tests/fixtures/ctx_example_2026-10-28.json")]) == 1
    dp = {"schema_version": "3.0", "doc_type": "DataPack", "team_id": "TEAM_042", "trade_date": "2026-10-28",
          "as_of_date": "2026-10-27", "generated_at": "2026-10-27T18:58:18+08:00", "missing": [],
          "prev_day_orders": {"outcome": "executed", "void_reasons": [], "excluded_codes": []},
          "account": {"cash": 6475000.0, "cash_ratio_tradable": None, "nav_tradable": 102000000.0, "aum": 102000000.0,
                      "receivable_cash": 0.0, "holdings_count": 1},
          "holdings": [{"code": "2330", "shares": 17000, "receivable_shares": 0, "close_price": 1480.0,
                        "tradable_weight": 0.246667}],
          "compliance": {"violations": 0, "notices": 0, "incidents": []}}
    p = tmp_path / "dp.json"
    p.write_text(json.dumps(dp), encoding="utf-8")
    ctx = vd.datapack_ctx(str(p), "2026-10-28", market_dir=tmp_path / "no_market")   # 沒有交易所行情 → 用 close_price
    assert ctx == {"prev_nav": 102000000.0, "cash": 6475000.0, "holdings": {"2330": 17000}, "prev_close": {"2330": 1480.0}}
    with pytest.raises(Exception, match="trade_date"):
        vd.datapack_ctx(str(p), "2026-10-29", market_dir=tmp_path / "no_market")
