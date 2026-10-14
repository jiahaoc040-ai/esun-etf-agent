"""人工收件匣：meta 檢查、指示性語句過濾、時間截止、格式、留存，以及事件能被 Agent 引用且 D-Plan 0 ERROR。"""
import json

import pytest

from esun_agent.data import inbox as ib

T, D = "2026-10-27", "2026-10-26"
META = {"url": "https://news.example.com/a/1", "authority": "media", "published_at": "2026-10-26T15:00:00+08:00",
        "title": "台積電公布九月營收"}


def put(base, name, text, meta=META, meta_name=None):
    d = base / T
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8") if isinstance(text, str) else (d / name).write_bytes(text)
    if meta is not None:
        mn = meta_name or (name.rsplit(".", 1)[0] + ".meta.json")
        (d / mn).write_text(json.dumps(meta, ensure_ascii=False) if isinstance(meta, dict) else meta, encoding="utf-8")


def load(tmp_path, **kw):
    return ib.load_inbox(T, D, base=tmp_path / "inbox", runs_dir=tmp_path / "runs", **kw)


def test_accepts_factual_docs_and_copies_raw(tmp_path):
    base = tmp_path / "inbox"
    put(base, "tsmc.md", "# 台積電公布九月營收\n台積電（2330）九月營收 3,200 億元，年增 32%，創單月新高。")
    put(base, "news.html", "<html><script>x=1</script><body><p>鴻海 2317 公告取得 AI 伺服器訂單。</p></body></html>",
        meta={**META, "title": "鴻海取得訂單", "url": "https://news.example.com/b"}, meta_name="news.html.meta.json")
    r = load(tmp_path)
    assert sorted(r.accepted) == ["news.html", "tsmc.md"] and r.warnings == []
    ev = {e["inbox_file"]: e for e in r.events}
    t = ev["tsmc.md"]
    assert t["authority"] == "media" and t["content_as_of"] == "2026-10-26T15:00:00+08:00" and t["basis"] is None
    assert t["statement"].startswith("台積電公布九月營收：") and "3,200 億元" in t["statement"] and len(t["statement"]) <= 500
    assert "2330" in t["tickers_mentioned"] and t["values"]["doc_chars"] > 0
    assert "x=1" not in ev["news.html"]["statement"] and "2317" in ev["news.html"]["tickers_mentioned"]
    raw = tmp_path / "runs" / T / "raw" / "inbox"
    assert {p.name for p in raw.iterdir()} == {"tsmc.md", "tsmc.meta.json", "news.html", "news.html.meta.json"}


@pytest.mark.parametrize("meta,needle", [
    (None, "缺少同名"),
    ({**META, "url": ""}, "url"),
    ({k: v for k, v in META.items() if k != "title"}, "title"),
    ({**META, "url": "ftp://x"}, "http"),
    ({**META, "authority": "blog"}, "authority"),
    ({**META, "published_at": "昨天"}, "published_at"),
    ({**META, "published_at": "2026-10-26T15:00:00"}, "時區"),
    ({**META, "published_at": "2026-10-26T20:00:00+08:00"}, "晚於決策日"),
    ({**META, "ticker": "9999"}, "不在 150 檔"),
    ({**META, "basis": "monthly_revenue", "ticker": "2330"}, "authority 須為 mops"),
    ({**META, "basis": "momentum", "ticker": "2330"}, "五種依據"),
    ({**META, "authority": "mops", "basis": "monthly_revenue"}, "ticker"),
    ({**META, "values": {"x": "多"}}, "數字"),
    ("{壞", "JSON"),
])
def test_skips_bad_meta_with_warning(tmp_path, meta, needle):
    put(tmp_path / "inbox", "a.txt", "台積電九月營收 3,200 億元。", meta=meta)
    r = load(tmp_path)
    assert r.events == [] and r.skipped == ["a.txt"]
    assert len(r.warnings) == 1 and needle in r.warnings[0] and "略過" in r.warnings[0], r.warnings
    assert (tmp_path / "runs" / T / "raw" / "inbox" / "a.txt").exists()          # 被略過的也留存


def test_directive_language_only_allowed_from_financial_institutions(tmp_path):
    base = tmp_path / "inbox"
    put(base, "tip.md", "分析師建議逢低買進聯發科，目標價 1,800 元。")
    r = load(tmp_path)
    assert r.events == [] and "指示性語句" in r.warnings[0] and "authority=media" in r.warnings[0]
    put(base, "tip.md", "分析師建議逢低買進聯發科，目標價 1,800 元。",
        meta={**META, "authority": "fininst", "title": "券商研究報告：聯發科"})
    r = load(tmp_path)
    assert r.accepted == ["tip.md"] and r.events[0]["authority"] == "fininst"
    put(base, "flow.md", "投信連續 5 日買超鴻海，累計 2 萬張。", meta={**META, "title": "投信動向"})
    assert "flow.md" in load(tmp_path).accepted                                   # 「買超」是事實，不是指示


def test_formats_and_unreadable_files(tmp_path):
    base = tmp_path / "inbox"
    put(base, "bad.pdf", b"%PDF-1.4 not really")
    put(base, "empty.txt", "   ")
    put(base, "sheet.xlsx", b"PK..")
    r = load(tmp_path)
    assert r.events == [] and sorted(r.skipped) == ["bad.pdf", "empty.txt", "sheet.xlsx"]
    msgs = " ".join(r.warnings)
    assert "PDF" in msgs and "讀不出文字" in msgs and "不支援的格式" in msgs


def test_pdf_text_extraction(tmp_path, monkeypatch):
    class Page:
        def extract_text(self):
            return "台積電 2330 法說會：第四季營收展望 2,900 億元"

    class Reader:
        def __init__(self, path):
            self.pages = [Page(), Page()]
    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    put(tmp_path / "inbox", "conf.pdf", b"%PDF", meta={**META, "authority": "mops", "basis": "investor_conference",
                                                        "ticker": "2330", "title": "台積電法說會", "values": {"q4_rev_twd_100m": 2900}})
    r = load(tmp_path)
    assert r.accepted == ["conf.pdf"]
    e = r.events[0]
    assert e["basis"] == "investor_conference" and e["ticker"] == "2330" and e["values"] == {"q4_rev_twd_100m": 2900}


def test_missing_folder_and_merge(tmp_path):
    r = load(tmp_path)
    assert r.events == [] and r.copied_to is None
    put(tmp_path / "inbox", "a.md", "台積電九月營收 3,200 億元。")
    r = load(tmp_path, copy_raw=False)
    assert r.copied_to is None and not (tmp_path / "runs").exists()
    base_inputs = {"events": [{"x": 1}], "taifex": {"a": 1}}
    out = ib.merge_into_inputs(base_inputs, r)
    assert len(out["events"]) == 2 and base_inputs["events"] == [{"x": 1}] and out["taifex"] == {"a": 1}


def test_cli(tmp_path, capsys):
    import importlib.util
    from pathlib import Path
    put(tmp_path / "inbox", "a.md", "台積電九月營收 3,200 億元。")
    put(tmp_path / "inbox", "b.md", "建議買進", meta={**META, "title": "x"})
    spec = importlib.util.spec_from_file_location("ingest", Path(__file__).parent.parent / "scripts" / "ingest_inbox.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main([T, "--prev-date", D, "--base", str(tmp_path / "inbox"), "--no-copy", "--json"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)[0]["inbox_file"] == "a.md" and "b.md" in out.err


def test_inbox_event_is_citable_and_dplan_validates(tmp_path):
    """收件匣事件進入 observations，Agent 在 market_view 引用它，D-Plan 0 ERROR 且來源保留 url／authority。"""
    from esun_agent.data.etf_holdings import load_top15
    from esun_agent.data.prices import load_panel
    from esun_agent.strategy.factors import compute_factors
    from esun_agent.strategy.marketcap import market_cap

    from .agent_helpers import ConvSim, FakeLLM, conviction_llm
    panel = load_panel()
    env = {"panel": panel, "factors": compute_factors(panel), "top15": load_top15(), "caps": market_cap(panel)[0]}
    trade, prev = "2026-09-29", panel.dates[panel.dates.index("2026-09-29") - 1]
    d = tmp_path / "inbox" / trade
    d.mkdir(parents=True)
    (d / "n.md").write_text("美國商務部公布 AI 晶片出口新規，自 11 月起實施。", encoding="utf-8")
    (d / "n.meta.json").write_text(json.dumps({"url": "https://news.example.com/policy", "authority": "media",
                                               "published_at": f"{prev}T16:00:00+08:00", "title": "AI 晶片出口新規"}),
                                   encoding="utf-8")
    r = ib.load_inbox(trade, prev, base=tmp_path / "inbox", runs_dir=tmp_path / "runs")
    assert r.accepted == ["n.md"]

    def gen(system, user):
        out = json.loads(conviction_llm()(system, user))
        oid = next(json.loads(l)["obs_id"] for l in user.splitlines()
                   if l.startswith('{"obs_id"') and "AI 晶片出口新規" in l)
        out["market_view"]["basis_refs"].append(oid)
        return json.dumps(out, ensure_ascii=False)
    sim = ConvSim(env, tmp_path)
    res = sim.day(trade, FakeLLM([gen]), extra_events=r.events)
    assert res.report.errors == [] and res.mode == "llm"
    src = [s for s in res.doc["sources"] if s["url"] == "https://news.example.com/policy"]
    assert src and src[0]["authority"] == "media" and src[0]["content_as_of"] == f"{prev}T16:00:00+08:00"
    assert any("AI 晶片出口新規" in o["statement"] for o in res.doc["observations"])
