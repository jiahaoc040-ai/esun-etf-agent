"""集中策略 D-Plan 組裝器：LLM 只給名單與信心等級，程式換算權重；紀律違規回饋重寫；降級保底；0 ERROR。"""
import json

import pytest

from esun_agent.agent import conviction_agent as ca
from esun_agent.agent import llm as L
from esun_agent.agent.conviction_state import ConvictionState, Pick
from esun_agent.config import ACTIVE_SHARE_MIN
from esun_agent.dplan_validate import validate

from .agent_helpers import ConvSim, FakeLLM, conviction_llm, find_obs, mv, prompt_candidates, prompt_current_picks

DAYS = ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05"]


@pytest.fixture(scope="module")
def env():
    from esun_agent.data.etf_holdings import load_top10
    from esun_agent.data.prices import load_panel
    from esun_agent.strategy.factors import compute_factors
    from esun_agent.strategy.marketcap import market_cap
    panel = load_panel()
    return {"panel": panel, "factors": compute_factors(panel), "top10": load_top10(), "caps": market_cap(panel)[0]}


def check_day(sim, r, T, held_before):
    assert r.report.errors == [], (T, r.report)
    doc = json.loads((sim.tmp / "out" / r.filename).read_text(encoding="utf-8"))
    assert doc == r.doc and doc["trade_date"] == T and doc["agent_metadata"]["code_version"].startswith("git:")
    covered = {d["ticker"] for d in doc["decisions"]} | {n["ticker"] for n in doc["no_trade_decisions"]}
    assert held_before <= covered
    w = sim.weights(T)
    assert 20 <= len(w) <= 30 and all(x <= (0.25 if t == "2330" else 0.10) for t, x in w.items())
    cash = sim.ledger.cash / sim.nav
    assert 0 <= cash < 0.25
    return w


# ------------------------------------------------------------------ 解析
def test_parse_picks_ok_and_rejects():
    good = {"market_view": {"regime": "neutral", "stance": "neutral", "logic": "x" * 30, "basis_refs": ["O1"],
                            "counter_evidence": None},
            "picks": [{"ticker": "2383", "conviction": "high", "basis": "trust_flow", "obs_ids": ["O3"], "logic": "y" * 30,
                       "counter_evidence": None}],
            "exits": [{"ticker": "3017", "obs_ids": ["O4"], "logic": "z" * 30}]}
    p, errs = L.parse_picks("```json\n" + json.dumps(good) + "\n```")
    assert errs == [] and p.levels() == [("2383", "high")] and p.exits[0].ticker == "3017"
    for mutate, needle in (
            (lambda d: d["picks"][0].update(weight=0.09), "不得輸出"),
            (lambda d: d["picks"][0].update(conviction="low"), "conviction"),
            (lambda d: d["picks"][0].update(basis="momentum"), "basis"),
            (lambda d: d["picks"].append(dict(d["picks"][0])), "重複"),
            (lambda d: d["exits"].append({"ticker": "2383", "obs_ids": ["O1"], "logic": "w" * 30}), "同時出現"),
            (lambda d: d.update(tilts=[]), "頂層只允許")):
        d = json.loads(json.dumps(good))
        mutate(d)
        p, errs = L.parse_picks(json.dumps(d))
        assert p is None and any(needle in e for e in errs), (needle, errs)
    assert L.parse_picks("不是 JSON")[1]


# ------------------------------------------------------------------ 5 日連續模擬
def test_five_day_simulation_zero_errors_and_rules(env, tmp_path):
    sim = ConvSim(env, tmp_path)
    res = {}
    for T in DAYS:
        before = set(sim.ledger.holdings)
        llm = FakeLLM([conviction_llm()])
        res[T] = r = sim.day(T, llm)
        assert r.mode == "llm" and len(r.attempts) == 1, (T, r.attempts)
        w = check_day(sim, r, T, before)
        assert 10 <= len(r.state.picks) <= 12 and 21 <= len(w) <= 24
        assert max(x for t, x in w.items() if t != "2330") < 0.10
    d1 = res[DAYS[0]]
    assert d1.doc["market_view"]["posture"]["net_exposure_intent"] == "increase" and d1.doc["no_trade_decisions"] == []
    picks = d1.state.picks
    assert all(p.since == DAYS[0] for p in picks.values())
    tw = {d["ticker"]: d["target_weight"] for d in d1.doc["decisions"]}
    lv = {t: p.level for t, p in picks.items()}
    for t, x in tw.items():                                         # 權重由信心等級換算
        if t in lv and t != "2330":
            assert x == pytest.approx(0.09 if lv[t] == "high" else 0.06, abs=0.012)
    # 每檔高信心部位的推論引用到該股的 trust_flow 事實
    obs = {o["obs_id"]: o for o in d1.doc["observations"]}
    for d in d1.doc["decisions"]:
        if d["ticker"] in picks:
            inf = next(i for i in d1.doc["inferences"] if i["inf_id"] == d["inference_refs"][0])
            assert any(obs[p]["topic"] == "trust_flow" for p in inf["premise_refs"])
    # 續抱者 since 不變
    last = res[DAYS[-1]].state.picks
    assert all(last[t].since == DAYS[0] for t in set(last) & set(picks))
    st = ConvictionState.load(sim.state_path)
    assert st.as_of == DAYS[-1] and st.mode == "llm" and st.prev_date == "2026-10-02"


def test_min_hold_and_swap_limit_are_fed_back(env, tmp_path):
    sim = ConvSim(env, tmp_path)
    sim.day(DAYS[0], FakeLLM([conviction_llm()]))
    first = list(sim.results[0].state.picks)
    bad_drop = conviction_llm(drop=first[:2])                      # 第 2 天就移出（未滿 3 日）
    r = sim.day(DAYS[1], FakeLLM([bad_drop, conviction_llm()]))
    assert len(r.attempts) == 2 and any("未觸發停損前不可賣出" in e for e in r.attempts[0]["errors"])
    assert set(first) <= set(r.state.picks)
    sim.day(DAYS[2], FakeLLM([conviction_llm()]))
    # 第 4 天（已持有 3 日）：移出 5 檔、補 5 檔 → 超過每日 4 檔 → 退回；改成移出 4 檔 → 通過
    many = conviction_llm(drop=first[:5])
    four = conviction_llm(drop=first[:4])
    r = sim.day(DAYS[3], FakeLLM([many, four]))
    assert any("超過每日換股上限" in e for e in r.attempts[0]["errors"]) and r.mode == "llm"
    assert not set(first[:4]) & set(r.state.picks)
    sold = {d["ticker"] for d in r.doc["decisions"] if d["action"] == "SELL_ALL"}
    assert set(first[:4]) <= sold
    exit_inf = [i for i in r.doc["inferences"] if "移出" in (i.get("conclusion") or "")]
    assert len(exit_inf) >= 4


def test_evidence_must_be_a_fact_of_that_stock(env, tmp_path):
    sim = ConvSim(env, tmp_path)

    def screen_only(system, user):
        d = json.loads(conviction_llm()(system, user))
        for p in d["picks"]:
            p["obs_ids"] = [find_obs(user, ticker=p["ticker"], topic="candidate_screen")]
        return json.dumps(d, ensure_ascii=False)

    def weights_out(system, user):
        d = json.loads(conviction_llm()(system, user))
        d["picks"][0]["weight"] = 0.09
        return json.dumps(d, ensure_ascii=False)
    r = sim.day(DAYS[0], FakeLLM([screen_only, weights_out, conviction_llm()]))
    assert r.mode == "llm" and len(r.attempts) == 3
    assert any("沒有任何一則是" in e for e in r.attempts[0]["errors"])
    assert any("不得輸出" in e for e in r.attempts[1]["errors"])


def test_stop_loss_forces_exit_unless_new_positive_event(env, tmp_path, monkeypatch):
    sim = ConvSim(env, tmp_path)
    sim.day(DAYS[0], FakeLLM([conviction_llm()]))
    victim = list(sim.results[0].state.picks)[0]
    real = ca.pick_return
    monkeypatch.setattr(ca, "pick_return", lambda panel, t, e, D: -0.15 if t == victim else real(panel, t, e, D))
    llm = FakeLLM([conviction_llm(), conviction_llm(drop=(victim,))])
    r = sim.day(DAYS[1], llm)
    assert "觸發停損" in llm.calls[0][1]
    assert any("停損" in e for e in r.attempts[0]["errors"])
    assert r.mode == "llm" and victim not in r.state.picks
    d = next(d for d in r.doc["decisions"] if d["ticker"] == victim)
    assert d["action"] == "SELL_ALL"
    obs = {o["obs_id"]: o for o in r.doc["observations"]}
    infs = {i["inf_id"]: i for i in r.doc["inferences"]}
    assert any(obs[p]["topic"] == "position_pnl" for ref in d["inference_refs"] for p in infs[ref]["premise_refs"])


def test_blackout_day_blocks_new_positions_and_freezes(env, tmp_path):
    sim = ConvSim(env, tmp_path)
    sim.day(DAYS[0], FakeLLM([conviction_llm()]))
    sim.day(DAYS[1], FakeLLM([conviction_llm()]))
    sim.day(DAYS[2], FakeLLM([conviction_llm()]))
    ev = [{"date": DAYS[3], "kind": "fomc", "name": "FOMC 利率決議"}]
    first = list(sim.results[0].state.picks)
    swap = conviction_llm(drop=first[:2])                          # 想換股 → 會新增 2 檔 → 事件前一日不可
    hold = conviction_llm()
    llm = FakeLLM([swap, hold])
    r = sim.day(DAYS[3], llm, events=ev)
    assert "高波動事件前一交易日" in llm.calls[0][1]
    assert any("不可新增部位" in e for e in r.attempts[0]["errors"]) and r.mode == "llm"
    assert not any(d["action"] == "BUY" for d in r.doc["decisions"])
    assert r.doc["no_trade_decisions"] and any(o["topic"] == "event_calendar" for o in r.doc["observations"])


def test_fallback_hold_then_base(env, tmp_path):
    sim = ConvSim(env, tmp_path)
    garbage = FakeLLM(["不是 JSON", "{", "[]"])
    r1 = sim.day(DAYS[0], garbage)                                  # 首日沒有名單 → 合規基準 A
    assert r1.mode == "base" and r1.report.errors == [] and len(r1.attempts) == 3
    assert "fallback_base_a" in r1.notes and r1.state.picks == {} and 25 <= len({o["ticker"] for o in r1.doc["orders"]}) <= 30
    r2 = sim.day(DAYS[1], FakeLLM([conviction_llm()]))              # 從合規基準 A 轉入集中組合（首次建倉不受換股上限）
    assert r2.mode == "llm" and 10 <= len(r2.state.picks) <= 12
    w2 = sim.weights(DAYS[1])
    assert 21 <= len(w2) <= 24
    class Boom(FakeLLM):
        def generate(self, system, user, **kw):
            self.calls.append((system, user))
            raise L.LLMError("HTTP 503")
    r3 = sim.day(DAYS[2], Boom([]))                                 # 有名單 → 凍結
    assert r3.mode == "hold" and r3.report.errors == [] and "fallback_hold" in r3.notes
    assert set(r3.state.picks) == set(r2.state.picks) and r3.state.last_fallback == DAYS[2]
    assert not any(d["action"] == "BUY" for d in r3.doc["decisions"])
    assert "凍結" in r3.doc["market_view"]["logic"]


def test_stance_must_match_direction(env, tmp_path):
    sim = ConvSim(env, tmp_path)
    r = sim.day(DAYS[0], FakeLLM([conviction_llm(regime="risk_off", stance="defensive"), conviction_llm()]))
    assert len(r.attempts) == 2 and any("stance=defensive" in e for e in r.attempts[0]["errors"])


def test_prompt_lists_candidates_2330_and_versions(env, tmp_path):
    sim = ConvSim(env, tmp_path)
    llm = FakeLLM([conviction_llm()])
    sim.day(DAYS[0], llm)
    system, user = llm.calls[0]
    assert "高" in system and "9%" in system and "6%" in system and "{{" not in system + user
    cands = prompt_candidates(user)
    assert 30 <= len(cands) <= 32 and "2330" in cands
    assert prompt_current_picks(user) == []
    llm2 = FakeLLM([conviction_llm()])
    sim.day(DAYS[1], llm2)
    assert len(prompt_current_picks(llm2.calls[0][1])) == len(sim.results[0].state.picks)


def test_write_refuses_documents_with_errors(env, tmp_path):
    from esun_agent.agent.assemble import AssembleError
    from esun_agent.dplan_validate import Report
    sim = ConvSim(env, tmp_path)
    r = sim.day(DAYS[0], FakeLLM([conviction_llm()]))
    r.report = Report(errors=["[C1] 人為製造的錯誤"])
    with pytest.raises(AssembleError, match="拒絕寫檔"):
        ca.write_conviction_dplan(r, tmp_path / "other")
    assert not (tmp_path / "other").exists()


def test_state_roundtrip(tmp_path):
    st = ConvictionState(picks={"2383": Pick("2383", "high", "2026-10-26", "monthly_revenue", "x" * 30, None, [], "2026-10-23T15:00:00+08:00", 0.6)},
                         fillers=["2330", "2317"], as_of="2026-10-27", prev_date="2026-10-26", mode="llm")
    p = st.save(tmp_path / "s.json")
    assert ConvictionState.load(p) == st
    assert ConvictionState.load(tmp_path / "none.json") == ConvictionState()


def test_real_data_proxy_window_no_violations(env):
    from esun_agent import conviction_backtest as cb
    i = env["panel"].dates.index("2026-08-03")
    r = cb.run_conviction_backtest(env["panel"], env["factors"], env["caps"], env["top10"], env["panel"].dates[i],
                                   env["panel"].dates[i + 23])
    assert r.violations["holdings"] == 0 and r.violations["cash"] == 0 and r.build_errors == 0
    assert (r.daily["ap_min"] >= ACTIVE_SHARE_MIN).all() and r.daily["n_hold"].between(21, 24).all()
