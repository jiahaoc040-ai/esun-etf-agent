import copy
import json

import docx
import pytest

from esun_agent import strategy_doc as sd
from esun_agent.agent.observations import BASIS_LABELS
from esun_agent.strategy.declaration import load_declaration

SUMMARY = {
    "oos_start": "2026-08-01", "oos_days": 44, "cap_source": "issued_shares", "universe_dates": ["2025-01-02", "2026-10-05"],
    "benchmarks_oos": {"a_equal_weight": 0.221, "b_cap_weight_capped": 0.208, "a_mdd": -0.05, "b_mdd": -0.043},
    "baseline_oos": {"total_return": 0.2101, "mdd": -0.0501, "ap_min": 0.218, "turnover_ann": 3.3, "cost_drag_ann": 0.019},
    "baseline_is": {"total_return": 1.19, "mdd": -0.21, "ap_min": 0.218, "turnover_ann": 2.2, "cost_drag_ann": 0.012},
    "ap_fix_tiers": {"minimal": 54, "wider": 18, "full": 1},
    "random_tilt": {"n": 100, "ret": {"mean": 0.199, "p5": 0.182, "p95": 0.217, "min": 0.17, "max": 0.238},
                    "excess_vs_b": {"mean": -0.009, "p5": -0.026, "p95": 0.008},
                    "mdd": {"mean": -0.049, "min": -0.056}, "ap_min_all": {"min": 0.172}, "turnover": {"mean": 6.7},
                    "violations": {"holdings": 0, "weight": 0, "cash": 0, "ap_below_20_days": 18, "ap_max_consecutive": 1}},
    "stress_daily_tilt": {"n": 30, "ret": {"mean": 0.178}, "turnover": {"mean": 28.2}},
}


def texts(d):
    out = [p.text for p in d.paragraphs]
    for t in d.tables:
        for r in t.rows:
            for c in r.cells:
                out.append(c.text)
    return "\n".join(out)


def build(tmp_path, **kw):
    return docx.Document(str(sd.build_strategy_doc(tmp_path / "doc.docx", summary=SUMMARY, **kw)))


def test_form_cells_come_from_declaration(tmp_path):
    decl = load_declaration()
    d = build(tmp_path)
    cells = [r.cells[1].text for r in d.tables[0].rows]
    assert cells == [decl["etf"]["name"], decl["theme"]["title"], decl["philosophy_statement"]]
    assert cells[0].startswith("主動") and len(cells[0]) <= 20 and len(cells[1]) <= 50 and 100 <= len(cells[2]) <= 300
    # 版型沿用官方範本：標題欄文字與頁首不變、表單欄位使用標楷體
    assert [r.cells[0].text for r in d.tables[0].rows][0].startswith("ETF 名稱")
    assert "參賽隊伍" in "".join(p.text for p in d.sections[0].header.paragraphs)
    run = d.tables[0].rows[0].cells[1].paragraphs[0].runs[0]
    assert run.font.name == "標楷體"


def test_refuses_noncompliant_declaration(tmp_path):
    decl = copy.deepcopy(load_declaration())
    decl["etf"]["name"] = "台灣核心優選 AI 主動 ETF"
    with pytest.raises(sd.DocError, match="開頭"):
        sd.build_strategy_doc(tmp_path / "x.docx", decl=decl, summary=SUMMARY)
    assert not (tmp_path / "x.docx").exists()
    decl = copy.deepcopy(load_declaration())
    decl["philosophy_statement"] = "短"
    with pytest.raises(sd.DocError, match="philosophy_statement"):
        sd.build_strategy_doc(tmp_path / "y.docx", decl=decl, summary=SUMMARY)


def test_form_only(tmp_path):
    d = build(tmp_path, form_only=True)
    assert "補充說明" not in texts(d) and len(d.tables) == 1


def test_supplement_content_and_numbers(tmp_path):
    decl = load_declaration()
    t = texts(build(tmp_path))
    for h in ("補充說明", "一、主體", "二、超額來源", "三、回測摘要", "四、每日 D-Plan"):
        assert h in t
    r = decl["portfolio_rules"]
    # 規則數字來自宣告檔
    assert f"{r['ap_target']:.0%}" in t and f"{r['ap_trigger']:.0%}" in t and f"{r['ap_recovery_target']:.0%}" in t
    assert f"{r['max_delta']:.0%}" in t and f"{r['max_active']:.0%}" in t and f"{r['band']:.0%}" in t
    for b in decl["tilt"]["allowed_bases"]:
        assert b["label"] in t and b["id"] in t
        for a in b["authority"]:
            assert a in t
    # 回測數字來自摘要，不是寫死
    for s in ("+22.1%", "+20.8%", "+21.0%", "-0.9%", "2026-08-01", "44 個交易日"):
        assert s in t, s
    assert "前視與倖存者偏誤" in t and "不證明 Agent" in t                      # 名單偏誤與限制必須寫明
    assert "連續 2 日" in t


def test_every_section_maps_to_dplan_fields(tmp_path):
    d = build(tmp_path)
    boxes = [t.rows[0].cells[0].text for t in d.tables if t.rows[0].cells[0].text.startswith("對應每日 D-Plan")]
    assert len(boxes) >= 4
    for b in boxes:
        assert "market_view" in b or "inferences" in b
    allt = "\n".join(boxes)
    for k in ("market_view", "inferences", "decisions", "observations", "no_trade_decisions"):
        assert k in allt


def test_mapping_matches_agent_code(env=None):
    """文件裡引用的 observation topic／依據 id 必須是 T4 程式真的會產生的。"""
    decl = load_declaration()
    assert {b["id"] for b in decl["tilt"]["allowed_bases"]} == set(BASIS_LABELS)
    assert {t for t, _ in sd.MARKET_TOPICS} == {"tw_market", "inst_flow", "taifex_night", "us_overnight"}
    from esun_agent.agent import observations as ob
    from esun_agent.data.prices import load_panel
    panel = load_panel()
    ev = ob.build_evidence(panel, "2026-10-02", inputs={
        "taifex": {"url": "https://x.example/t", "content_as_of": "2026-10-02T05:00:00+08:00", "close": 1.0, "chg_pct": 0.1},
        "us": {"url": "https://x.example/u", "content_as_of": "2026-10-02T04:00:00+08:00", "quotes": {"SOX": {"chg_pct": 1.0}}}})
    assert {t for t, _ in sd.MARKET_TOPICS} <= {o["topic"] for o in ev.observations}


def test_cli(tmp_path, capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("make_doc", Path(__file__).parent.parent / "scripts" / "make_strategy_doc.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--out", str(tmp_path / "o.docx"), "--form-only"]) == 0
    assert (tmp_path / "o.docx").exists()
    missing = sd.SUMMARY_PATH
    if not missing.exists():
        assert mod.main(["--out", str(tmp_path / "p.docx")]) == 1


def test_real_summary_file_builds_when_present(tmp_path):
    if not sd.SUMMARY_PATH.exists():
        pytest.skip("尚未產生 docs/final_strategy_backtest.json")
    S = sd.load_summary()
    d = docx.Document(str(sd.build_strategy_doc(tmp_path / "real.docx", summary=S)))
    assert pct_in(S["baseline_oos"]["total_return"], texts(d))


def pct_in(x, t):
    return sd.pct(x) in t
