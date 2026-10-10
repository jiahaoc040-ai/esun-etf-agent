import json
import re

import pytest

from esun_agent.agent import assemble as asm
from esun_agent.agent import llm as L
from esun_agent.agent import observations as ob
from esun_agent.agent import plan as pl
from esun_agent.agent import tilt_state as ts
from esun_agent.dplan_validate import validate
from esun_agent.strategy.declaration import load_declaration

from .agent_helpers import FakeLLM, Sim, find_obs, load_inputs, mv, obs_in_prompt


@pytest.fixture(scope="module")
def env():
    from esun_agent.data.etf_holdings import load_top10
    from esun_agent.data.prices import load_panel
    from esun_agent.strategy.factors import compute_factors
    from esun_agent.strategy.marketcap import market_cap
    panel = load_panel()
    return {"panel": panel, "factors": compute_factors(panel), "top10": load_top10(), "caps": market_cap(panel)[0]}


D1, T1 = "2026-09-24", "2026-09-29"
DAYS = ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05"]


# ------------------------------------------------------------------ observations
def test_values_must_be_numbers():
    assert ob.clean_values({"a": 1, "b": 2.5}) == {"a": 1, "b": 2.5}
    for bad in ({"a": "x"}, {"a": None}, {"a": True}, {"a": float("nan")}, {"a": float("inf")}):
        with pytest.raises(ValueError):
            ob.clean_values(bad)


def test_build_evidence_sources_authority_and_ids(env):
    inputs = load_inputs(T1)
    ev = ob.build_evidence(env["panel"], D1, inputs=inputs, focus_tickers=["2330", "2317", "3443"])
    assert [s["source_id"] for s in ev.sources] == [f"S{i}" for i in range(1, len(ev.sources) + 1)]
    assert [o["obs_id"] for o in ev.observations] == [f"O{i}" for i in range(1, len(ev.observations) + 1)]
    auth = {s["authority"] for s in ev.sources}
    assert {"twse", "tpex", "taifex", "vendor", "mops", "media"} <= auth
    mk = [s for s in ev.sources if "日行情" in s.get("name", "")]
    assert {s["authority"] for s in mk} == {"twse", "tpex"}                 # 上市／上櫃分開
    for o in ev.observations:
        assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in o["values"].values())
        assert all(re.fullmatch(r"S\d{1,3}", r) for r in o["source_ref"]) and 5 <= len(o["statement"]) <= 500
    rev = next(o for o in ev.observations if o.get("topic") == "monthly_revenue")
    assert ev.meta[rev["obs_id"]]["ticker"] == "2317" and ev.meta[rev["obs_id"]]["authorities"] == ["mops"]
    news = next(o for o in ev.observations if o.get("topic") == "news")
    assert ev.source_of(news["obs_id"])[0]["archive_url"].startswith("https://web.archive.org")
    # 所有 source 的 content_as_of 都是決策日當天或之前
    assert all(s["content_as_of"][:10] <= D1 for s in ev.sources)


def test_trust_flow_authority_follows_market(env):
    ev = ob.build_evidence(env["panel"], "2026-10-02", focus_tickers=list(env["panel"].close.columns))
    tf = ev.by_topic("trust_flow")
    assert tf, "名單內應有投信連續買賣超 ≥3 日的個股"
    from esun_agent.universe import authority_for
    for o in tf:
        t = ev.meta[o["obs_id"]]["ticker"]
        assert ev.meta[o["obs_id"]]["authorities"] == [authority_for(t)]
        assert o["values"]["streak_days"] != 0 and abs(o["values"]["streak_days"]) >= 3


def test_event_authority_must_match_declaration():
    b = ob.EvidenceBuilder()
    base = {"basis": "monthly_revenue", "ticker": "2317", "url": "https://x.example/1", "content_as_of": "2026-09-24T15:00:00+08:00",
            "statement": "鴻海 8 月營收年增 12.3%", "values": {"yoy_pct": 12.3}}
    ob.event_observation(b, {**base, "authority": "mops"})
    with pytest.raises(ValueError, match="authority"):
        ob.event_observation(b, {**base, "authority": "media"})            # 月營收只允許 mops
    ob.event_observation(b, {**base, "basis": "investor_conference", "authority": "media"})   # 法說允許 mops／media
    with pytest.raises(ValueError, match="150"):
        ob.event_observation(b, {**base, "authority": "mops", "ticker": "9999"})
    with pytest.raises(ValueError):
        ob.event_observation(b, {**base, "authority": "mops", "basis": "price_trend"})


def test_prune_renumbers_and_remaps(env):
    ev = ob.build_evidence(env["panel"], D1, inputs=load_inputs(T1))
    keep = {ev.by_topic("us_overnight")[0]["obs_id"], ev.by_topic("tw_market")[0]["obs_id"]}
    sources, observations, omap = ob.prune(ev, keep)
    assert [o["obs_id"] for o in observations] == ["O1", "O2"] and sorted(omap.values()) == ["O1", "O2"]
    assert [s["source_id"] for s in sources] == [f"S{i}" for i in range(1, len(sources) + 1)]
    used = {r for o in observations for r in o["source_ref"]}
    assert used == {s["source_id"] for s in sources}


# ------------------------------------------------------------------ tilt 狀態與持有規則
CAL = [f"2026-09-{d:02d}" for d in (21, 22, 23, 24, 25, 28, 29, 30)] + ["2026-10-01", "2026-10-02", "2026-10-05"]


def tilt(delta=0.02, since="2026-09-29", asof="2026-09-24T15:00:00+08:00"):
    return ts.ActiveTilt("2317", delta, since, "monthly_revenue", "x" * 30, None, [], asof)


def test_trading_days_elapsed():
    assert ts.trading_days_elapsed(CAL, "2026-09-29", "2026-09-30") == 1
    assert ts.trading_days_elapsed(CAL, "2026-09-29", "2026-10-05") == 4
    assert ts.trading_days_elapsed(CAL[:-1], "2026-09-29", "2026-10-05") == 4       # today 尚未在日曆內 → 視為下一個交易日


def test_tilt_change_rules():
    cur = tilt()
    assert ts.check_change(cur, 0.02, None, False, CAL, "2026-10-01") == []                     # 沒有變更
    e = ts.check_change(cur, 0.0, "negative", False, CAL, "2026-10-01")
    assert any("無新事件" in x for x in e)
    assert ts.check_change(cur, 0.0, "negative", True, CAL, "2026-10-01") == []                 # 新事件＋反向 → 可提前撤銷
    e = ts.check_change(cur, 0.0, "positive", True, CAL, "2026-10-01")
    assert any("方向相反" in x for x in e)
    e = ts.check_change(cur, 0.03, "positive", True, CAL, "2026-10-01")
    assert any("同方向加碼" in x for x in e)
    assert ts.check_change(cur, -0.01, "negative", True, CAL, "2026-10-01") == []              # 反向
    old = tilt(since="2026-09-21")                                                              # 持有 ≥5 日
    assert ts.check_change(old, 0.03, "positive", True, CAL, "2026-10-01") == []
    assert any("無新事件" in x for x in ts.check_change(old, 0.03, "positive", False, CAL, "2026-10-01"))
    assert ts.check_change(None, 0.01, "positive", True, CAL, "2026-10-01") == []
    assert any("新事件" in x for x in ts.check_change(None, 0.01, "positive", False, CAL, "2026-10-01"))
    assert any("方向" in x for x in ts.check_change(None, 0.01, "negative", True, CAL, "2026-10-01"))


def test_state_roundtrip(tmp_path):
    st = ts.TiltState(tilts={"2317": tilt()}, last_rebalance="2026-09-29", as_of="2026-09-29")
    p = st.save(tmp_path / "s" / "tilts.json")
    back = ts.TiltState.load(p)
    assert back.tilts["2317"].delta == 0.02 and back.last_rebalance == "2026-09-29" and back.deltas() == {"2317": 0.02}
    assert ts.TiltState.load(tmp_path / "none.json").tilts == {}


# ------------------------------------------------------------------ LLM 輸出解析與供應商
GOOD = {"market_view": {"regime": "neutral", "stance": "neutral", "logic": "x" * 25, "basis_refs": ["O1"],
                        "counter_evidence": None, "confidence": 0.5},
        "tilts": [{"ticker": "2317", "delta": 0.02, "basis": "monthly_revenue", "event_direction": "positive",
                   "obs_ids": ["O3"], "logic": "y" * 25, "counter_evidence": None, "confidence": 0.5}]}


def test_parse_proposal_ok_and_fenced():
    p, errs = L.parse_proposal(json.dumps(GOOD))
    assert errs == [] and p.tilts[0].delta == 0.02 and p.market_view.regime == "neutral"
    p2, errs2 = L.parse_proposal("```json\n" + json.dumps(GOOD) + "\n```")
    assert errs2 == [] and p2.tilts[0].ticker == "2317"
    p3, errs3 = L.parse_proposal("好的，以下是結果：" + json.dumps(GOOD) + " 完畢")
    assert errs3 == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["tilts"][0].update(target_weight=0.05), "target_weight"),
    (lambda d: d["tilts"][0].update(shares=1000), "shares"),
    (lambda d: d["market_view"].update(posture={"net_exposure_intent": "hold"}), "posture"),
    (lambda d: d.update(orders=[]), "orders"),
    (lambda d: d["market_view"].update(regime="bullish"), "regime"),
    (lambda d: d["market_view"].update(basis_refs=[]), "basis_refs"),
    (lambda d: d["tilts"][0].update(delta="0.02"), "delta"),
    (lambda d: d["tilts"][0].update(basis="price_trend"), "basis"),
    (lambda d: d["tilts"][0].update(event_direction="up"), "event_direction"),
    (lambda d: d["tilts"][0].update(obs_ids=[]), "obs_ids"),
    (lambda d: d["tilts"][0].update(logic="太短"), "logic"),
    (lambda d: d["tilts"].append(dict(d["tilts"][0])), "重複"),
])
def test_parse_proposal_rejects(mutate, needle):
    d = json.loads(json.dumps(GOOD))
    mutate(d)
    p, errs = L.parse_proposal(json.dumps(d))
    assert p is None and any(needle in e for e in errs), errs


def test_parse_proposal_garbage():
    for t in ("不是 JSON", "[]", '{"tilts": []}'):
        p, errs = L.parse_proposal(t)
        assert p is None and errs


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p


class Sess:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.resp


def test_gemini_client(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(L.LLMError, match="GOOGLE_API_KEY"):
        L.GeminiClient()
    s = Sess(Resp(200, {"candidates": [{"content": {"parts": [{"text": "{\"a\": 1}"}]}}],
                        "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7}}))
    c = L.GeminiClient(api_key="k", model="gemini-test", session=s)
    r = c.generate("sys", "usr")
    assert (r.text, r.input_tokens, r.output_tokens) == ('{"a": 1}', 11, 7) and c.provider == "google"
    url, kw = s.calls[0]
    assert url.endswith("/models/gemini-test:generateContent") and kw["params"] == {"key": "k"}
    assert kw["json"]["generationConfig"]["responseMimeType"] == "application/json"
    assert kw["json"]["systemInstruction"]["parts"][0]["text"] == "sys"
    with pytest.raises(L.LLMError, match="HTTP 429"):
        L.GeminiClient(api_key="k", session=Sess(Resp(429, {"error": "quota"}))).generate("s", "u")


def test_anthropic_client(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(L.LLMError, match="ANTHROPIC_API_KEY"):
        L.AnthropicClient()
    s = Sess(Resp(200, {"content": [{"type": "text", "text": "{}"}], "usage": {"input_tokens": 5, "output_tokens": 3}}))
    c = L.AnthropicClient(api_key="k", model="claude-test", session=s)
    r = c.generate("sys", "usr")
    assert (r.text, r.input_tokens, r.output_tokens) == ("{}", 5, 3) and c.provider == "anthropic"
    url, kw = s.calls[0]
    assert url == "https://api.anthropic.com/v1/messages" and kw["headers"]["x-api-key"] == "k"
    assert kw["json"]["system"] == "sys" and kw["json"]["messages"][0]["content"] == "usr"


def test_make_client_switch(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert L.make_client().provider == "google"                                   # 預設 Gemini
    assert L.make_client("anthropic").provider == "anthropic"
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert L.make_client().provider == "anthropic"
    with pytest.raises(L.LLMError):
        L.make_client("nope")


# ------------------------------------------------------------------ 委託、posture、funding
NAV = 1e9
CLOSE = {"A": 100.0, "B": 50.0, "C": 20.0}


def test_cash_floor_scales_buys_only():
    # 現金 3%，目標買進 B 10%、C 5%：不縮減的話現金為負
    plan = pl.plan_orders({"B": 0.10, "C": 0.05}, NAV, 0.03 * NAV, CLOSE, {})
    assert plan.scale < 1 and plan.cash_after / NAV >= 0.02 - 1e-6 and any(n.startswith("buys_scaled") for n in plan.notes)
    ok = pl.plan_orders({"B": 0.01}, NAV, 0.03 * NAV, CLOSE, {})
    assert ok.scale == 1.0 and not ok.notes
    # 賣出不受縮減影響
    held = {"A": 5_000_000}                                                      # 50% NAV
    p3 = pl.plan_orders({"A": 0.30, "B": 0.25}, NAV, 0.03 * NAV, CLOSE, held)
    sells = [o for o in p3.orders if o.side == "SELL"]
    assert sells and sells[0].shares == 2_000_000 and p3.cash_after / NAV >= 0.02 - 1e-6


def test_no_trade_when_target_equals_current_weights():
    held = {"A": 1_234_000, "B": 987_000}
    cur = {t: n * CLOSE[t] / NAV for t, n in held.items()}
    plan = pl.plan_orders(cur, NAV, 0.5 * NAV, CLOSE, held)
    assert plan.decisions == [] and plan.orders == [] and sorted(plan.no_trade) == ["A", "B"]


def test_posture_follows_actual_orders():
    inc = pl.plan_orders({"B": 0.05}, NAV, 0.5 * NAV, CLOSE, {})
    assert pl.posture_for(inc, NAV)["net_exposure_intent"] == "increase"
    held = {"A": 5_000_000}
    red = pl.plan_orders({"A": 0.40}, NAV, 0.05 * NAV, CLOSE, held)
    assert pl.posture_for(red, NAV)["net_exposure_intent"] == "reduce"
    small = pl.plan_orders({"A": 0.49}, NAV, 0.05 * NAV, CLOSE, held)
    p = pl.posture_for(small, NAV)
    assert p["net_exposure_intent"] == "hold"
    c = small.cash_after / NAV
    lo, hi = p["target_cash_pct_range"]
    assert lo <= c <= hi and lo >= 0.01 - 1e-9 and hi <= 0.22 + 1e-9


def _dec_ids(plan):
    return [{"decision_id": f"D{i}", **d} for i, d in enumerate(plan.decisions, 1)]


def test_funding_for_only_when_gap_really_exists():
    held = {"A": 10_000_000 // 2}                                                # 50% NAV
    plan = pl.plan_orders({"A": 0.40, "B": 0.10}, NAV, 0.03 * NAV, CLOSE, held)  # 賣 A 10% 買 B 10%
    decs = _dec_ids(plan)
    lo = pl.posture_for(plan, NAV)["target_cash_pct_range"][0]
    pl.assign_funding(decs, plan, NAV, CLOSE, lo)
    a = next(d for d in decs if d["ticker"] == "A")
    assert a["funding_for"] == [next(d["decision_id"] for d in decs if d["ticker"] == "B")]
    # 賣 A 1%、買 B 1%：拿掉賣出後現金只少 1% < 2% 區間寬度 → 沒有缺口 → 不標記
    plan2 = pl.plan_orders({"A": 0.49, "B": 0.01}, NAV, 0.03 * NAV, CLOSE, held)
    decs2 = _dec_ids(plan2)
    pl.assign_funding(decs2, plan2, NAV, CLOSE, pl.posture_for(plan2, NAV)["target_cash_pct_range"][0])
    assert all("funding_for" not in d for d in decs2)
    # 沒有買進 → 不標記
    plan3 = pl.plan_orders({"A": 0.30}, NAV, 0.03 * NAV, CLOSE, held)
    decs3 = _dec_ids(plan3)
    pl.assign_funding(decs3, plan3, NAV, CLOSE, 0.01)
    assert all("funding_for" not in d for d in decs3)


# ------------------------------------------------------------------ prompt 與版本
def test_prompts_versioned_and_render(env):
    v = asm.prompt_version()
    assert re.fullmatch(r"[0-9a-f]{8}", v)
    assert (asm.PROMPT_DIR / "system.md").exists() and (asm.PROMPT_DIR / "user.md").exists()
    assert re.fullmatch(r"git:[0-9a-f]{7,}(-dirty)?|git:unknown", asm.code_version())


def test_code_version_override(monkeypatch):
    monkeypatch.setenv("CODE_VERSION", "git:abc1234")
    assert asm.code_version() == "git:abc1234"


# ------------------------------------------------------------------ 5 個交易日連續模擬
def tilt_json(user, ticker, delta, basis, topic, direction, logic, ce=None):
    return {"ticker": ticker, "delta": delta, "basis": basis, "event_direction": direction,
            "obs_ids": [find_obs(user, ticker=ticker, topic=topic, carried=False)], "logic": logic,
            "counter_evidence": ce, "confidence": 0.55}


def day1(system, user):
    return json.dumps({"market_view": mv(user), "tilts": [
        tilt_json(user, "2317", 0.02, "monthly_revenue", "monthly_revenue", "positive",
                  "鴻海 8 月營收創單月新高且年增 12.3%，符合月營收加速的加碼依據，於基準上加碼 2%，風險為單月數字波動。"),
        tilt_json(user, "1101", 0.015, "material_news", "material_news", "positive",
                  "台泥處分海外水泥廠預計認列約 35 億元利益，屬重大訊息的正面事件；該股不在基準前 28 名，故新建小部位。",
                  "處分利益為一次性，不改變營運")]}, ensure_ascii=False)


def day2_bad(system, user):                        # 第一次：證據正確但 |delta| 5% > 3% → apply_tilts TiltError → 回饋重試
    return json.dumps({"market_view": mv(user), "tilts": [
        {"ticker": "2412", "delta": 0.05, "basis": "material_news", "event_direction": "positive",
         "obs_ids": [find_obs(user, ticker="2412", topic="material_news")],
         "logic": "中華電簽訂五年期雲端專線合約約 12 億元，屬重大訊息正面事件，欲加碼 5%（超過單檔上限，應被退回）。",
         "counter_evidence": None}]}, ensure_ascii=False)


def day2_blocked(system, user):                    # 第二次：想撤銷 2317 但沒有新事件 → 規則錯誤再重試
    return json.dumps({"market_view": mv(user), "tilts": [
        {"ticker": "2317", "delta": 0.0, "basis": "monthly_revenue", "event_direction": "negative",
         "obs_ids": [find_obs(user, ticker="2317", topic="monthly_revenue")], "logic": "w" * 30, "counter_evidence": None}]},
        ensure_ascii=False)


def day_nochange(system, user):
    return json.dumps({"market_view": mv(user), "tilts": []}, ensure_ascii=False)


def day3_revoke(system, user):                     # 新的負面重大訊息 → 持有未滿 5 日也可撤銷 2317
    return json.dumps({"market_view": mv(user, "neutral", "neutral"), "tilts": [
        tilt_json(user, "2317", 0.0, "material_news", "material_news", "negative",
                  "鴻海公告主要客戶下修第四季訂單預估 8%，與原先月營收加碼的前提相反，依新事件提前撤銷 tilt，回到基準權重。",
                  "單一客戶下修未必代表整體需求轉弱")]}, ensure_ascii=False)


def day5_new(system, user):
    return json.dumps({"market_view": mv(user), "tilts": [
        tilt_json(user, "2330", -0.015, "financial_report", "financial_report", "negative",
                  "台積電第三季毛利率 58.2%，較前季下降 1.1 個百分點，屬財報面的轉弱訊號，在基準上小幅減碼 1.5%。",
                  "毛利率仍高於公司長期目標")]}, ensure_ascii=False)


def test_five_day_simulation_all_zero_errors(env, tmp_path):
    sim = Sim(env, tmp_path)
    scripts = {
        "2026-09-29": FakeLLM([day1]),
        "2026-09-30": FakeLLM([day2_bad, day2_blocked, day_nochange]),
        "2026-10-01": FakeLLM([day3_revoke]),
        "2026-10-02": FakeLLM(["這不是 JSON", "{壞掉", "[]"]),
        "2026-10-05": FakeLLM([day5_new]),
    }
    res, held_before = {}, {}
    for T in DAYS:
        held_before[T] = set(sim.ledger.holdings)
        res[T] = r = sim.day(T, scripts[T])
        assert r.report.errors == [], (T, r.report)
        # 檔案落地，內容與回傳一致；再用主辦方式的 context 獨立重驗證一次 0 ERROR
        doc = json.loads((tmp_path / "out" / r.filename).read_text(encoding="utf-8"))
        assert doc == r.doc and doc["trade_date"] == T and doc["team_id"] == "TEAM_042"
        assert doc["agent_metadata"]["code_version"].startswith("git:")
        assert len({d["decision_id"] for d in doc["decisions"]}) == len(doc["decisions"])
        covered = {d["ticker"] for d in doc["decisions"]} | {n["ticker"] for n in doc["no_trade_decisions"]}
        assert held_before[T] <= covered                                     # 昨日每一檔持股都有交代

    # Day1：建倉，tilt 生效，持股 20–30，沒有 no_trade
    d1 = res["2026-09-29"]
    assert not d1.fallback and len(d1.attempts) == 1 and d1.rebalanced
    assert 20 <= len({o["ticker"] for o in d1.doc["orders"]}) <= 30
    assert {"2317", "1101"} <= {d["ticker"] for d in d1.doc["decisions"]}
    assert d1.doc["no_trade_decisions"] == [] and d1.doc["market_view"]["posture"]["net_exposure_intent"] == "increase"
    assert set(d1.state.tilts) == {"2317", "1101"} and d1.state.last_rebalance == "2026-09-29"
    assert d1.doc["agent_metadata"]["input_tokens"] == 1000
    # Day2：LLM 兩次不合法（TiltError、無新事件撤銷）→ 第 3 次成功；錯誤有回饋給 LLM
    d2 = res["2026-09-30"]
    assert not d2.fallback and len(d2.attempts) == 3
    assert any("apply_tilts" in e and "單檔上限" in e for e in d2.attempts[0]["errors"])
    assert any("無新事件" in e for e in d2.attempts[1]["errors"])
    llm2 = scripts["2026-09-30"]
    assert "apply_tilts" in llm2.calls[1][1] and "無新事件" in llm2.calls[2][1]
    assert d2.doc["orders"] == [] and d2.doc["decisions"] == [] and not d2.rebalanced   # 非再平衡日、tilt 沒變 → 全部 no_trade
    assert {n["ticker"] for n in d2.doc["no_trade_decisions"]} == held_before["2026-09-30"]
    assert d2.doc["agent_metadata"]["input_tokens"] == 3000
    # Day3：新負面事件 → 提前撤銷 2317（tilt 變動 → 當天可交易），1101 照舊
    d3 = res["2026-10-01"]
    assert not d3.fallback and d3.rebalanced and "2317" not in d3.state.tilts and "1101" in d3.state.tilts
    assert any(d["ticker"] == "2317" for d in d3.doc["decisions"]) or d3.doc["orders"]
    # Day4：LLM 連三次壞輸出 → 保底（零 tilt），一樣 0 ERROR；既有 tilt 狀態不被破壞
    d4 = res["2026-10-02"]
    assert d4.fallback and len(d4.attempts) == 3 and "fallback_zero_tilt" in d4.notes
    assert d4.state.last_fallback == "2026-10-02" and "1101" in d4.state.tilts
    assert d4.doc["market_view"]["regime"] == "neutral" and not d4.rebalanced
    assert all("不是 JSON" not in json.dumps(d4.doc, ensure_ascii=False) for _ in [0])
    # Day5：2330 新增減碼 tilt（tilt 變動 → 再平衡）、1101 沿用（持有未滿 5 日、無新事件）
    d5 = res["2026-10-05"]
    assert not d5.fallback and "2330" in d5.state.tilts and "1101" in d5.state.tilts and d5.rebalanced
    assert any(d["ticker"] == "2330" and d["action"] in ("TRIM", "SELL_ALL") for d in d5.doc["decisions"])
    carried_inf = [i for i in d5.doc["inferences"] if "沿用" in i["logic"]]
    assert carried_inf and all(i["premise_refs"] for i in carried_inf)
    # 狀態檔已寫入最後一天
    st = ts.TiltState.load(sim.state_path)
    assert st.as_of == "2026-10-05" and set(st.tilts) == {"2330", "1101"}


def test_stance_must_match_actual_direction(env, tmp_path):
    """宣告 defensive 卻因淨買超使 posture=increase → 退回重試；第二次改 neutral 通過。"""
    sim = Sim(env, tmp_path)
    first = lambda s, u: json.dumps({"market_view": mv(u, "risk_off", "defensive"), "tilts": []}, ensure_ascii=False)  # noqa: E731
    second = lambda s, u: json.dumps({"market_view": mv(u, "neutral", "neutral"), "tilts": []}, ensure_ascii=False)   # noqa: E731
    llm = FakeLLM([first, second])
    r = sim.day("2026-09-29", llm)                                              # 第一天建倉 → increase
    assert not r.fallback and len(r.attempts) == 2 and any("stance=defensive" in e for e in r.attempts[0]["errors"])
    assert r.doc["market_view"]["stance"] == "neutral"


def test_llm_api_failure_counts_as_attempt_then_fallback(env, tmp_path):
    class Boom(FakeLLM):
        def generate(self, system, user, **kw):
            self.calls.append((system, user))
            raise L.LLMError("HTTP 503")
    sim = Sim(env, tmp_path)
    llm = Boom([])
    r = sim.day("2026-09-29", llm)
    assert r.fallback and len(llm.calls) == 3 and r.report.errors == []
    assert all("LLM 呼叫失敗" in a["errors"][0] for a in r.attempts)
    assert r.doc["inferences"] and r.doc["agent_metadata"]["input_tokens"] == 0


def test_invalid_evidence_is_rejected_and_never_written(env, tmp_path):
    """引用別檔的事實、或 authority 不符的 observation → 退回；不會產出檔案。"""
    sim = Sim(env, tmp_path)
    def wrong_ticker(system, user):
        return json.dumps({"market_view": mv(user), "tilts": [
            {"ticker": "2330", "delta": 0.01, "basis": "monthly_revenue", "event_direction": "positive",
             "obs_ids": [find_obs(user, ticker="2317", topic="monthly_revenue")], "logic": "q" * 30,
             "counter_evidence": None}]}, ensure_ascii=False)
    llm = FakeLLM([wrong_ticker])
    r = sim.day("2026-09-29", llm)
    assert r.fallback and all(any("沒有任何一則是" in e for e in a["errors"]) for a in r.attempts)
    assert r.state.tilts == {}


def test_write_refuses_documents_with_errors(env, tmp_path):
    from esun_agent.dplan_validate import Report
    sim = Sim(env, tmp_path)
    r = sim.day("2026-09-29", FakeLLM([day1]))
    r.report = Report(errors=["[C1] 人為製造的錯誤"])
    with pytest.raises(asm.AssembleError, match="拒絕寫檔"):
        asm.write_dplan(r, tmp_path / "other")
    assert not (tmp_path / "other").exists()
