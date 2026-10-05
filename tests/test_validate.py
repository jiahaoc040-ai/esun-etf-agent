import copy
import json
from pathlib import Path

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
