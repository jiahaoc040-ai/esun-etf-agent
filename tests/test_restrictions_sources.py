"""處置／注意／停牌限制、全市場三大法人金額（BFI82U）、聯準會新聞稿 RSS、FRED DEXTAUS。"""
import json
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pytest

from esun_agent.agent import conviction_agent as ca
from esun_agent.agent import observations as ob
from esun_agent.collect import fed, restrictions as rs

from .agent_helpers import ConvSim, FakeLLM, conviction_llm, obs_in_prompt, prompt_candidates
from .test_collect import fetcher
from .test_conviction_agent import DAYS, check_day, env  # noqa: F401 - env 為 module fixture

FX = Path(__file__).parent / "fixtures" / "collect"
TZ = timezone(timedelta(hours=8))
UNI = {t: {"name": t, "market": m} for t, m in (("3017", "TWSE"), ("2330", "TWSE"), ("2454", "TWSE"), ("2883", "TWSE"),
                                                ("2317", "TWSE"), ("3443", "TPEX"), ("6488", "TPEX"))}


def fxb(name):
    return (FX / name).read_bytes()


ROUTES = [("announcement/punish", fxb("restrict_twse_punish.json")), ("announcement/notice", fxb("restrict_twse_notice.json")),
          ("TWTAWU", fxb("restrict_twse_twtawu.json")), ("tpex_disposal_information", fxb("restrict_tpex_disposal.json")),
          ("tpex_trading_warning_information", fxb("restrict_tpex_warning.json"))]


# ------------------------------------------------------------------ 交易限制收集
def test_collect_restrictions_both_markets_filtered_by_period_and_universe():
    r, warns = rs.collect_restrictions(fetcher(ROUTES), date(2026, 10, 19), UNI)
    assert warns == []
    assert set(r["disposal"]) == {"3017", "3443"}                         # 2330 處置期已過（10/01–10/14）；9999 不在 universe
    assert r["disposal"]["3017"]["period"] == ["2026-10-19", "2026-10-30"] and r["disposal"]["3017"]["authority"] == "twse"
    assert r["disposal"]["3443"]["period"] == ["2026-10-19", "2026-10-30"] and r["disposal"]["3443"]["authority"] == "tpex"
    assert set(r["attention"]) == {"2454", "6488"}
    assert set(r["suspended"]) == {"2883"}                                # 2317 已於 10/15 恢復交易
    assert {(s["authority"], s["kind"]) for s in r["sources"]} == set(rs.ENDPOINTS)
    early, _ = rs.collect_restrictions(fetcher(ROUTES), date(2026, 10, 16), UNI)
    assert "3017" not in early["disposal"]                                # 處置期 10/19 才開始


def test_collect_restrictions_endpoint_failure_only_warns():
    routes = [x for x in ROUTES if "notice" not in x[0]] + [("announcement/notice", b"<html>busy</html>")]
    r, warns = rs.collect_restrictions(fetcher(routes), date(2026, 10, 19), UNI)
    assert r["attention"].keys() == {"6488"} and len(warns) == 1 and "上市注意股票抓取失敗" in warns[0]
    with pytest.raises(ValueError, match="不是陣列"):
        rs.parse_records({"error": "x"}, "disposal", date(2026, 10, 19), UNI)


def test_active_and_apply_restrictions():
    r = {"disposal": {"A": {"period": ["2026-10-19", "2026-10-30"]}, "B": {}},
         "attention": {"C": {}, "D": {}}, "suspended": {"E": {}, "F": {}}}
    act = ca.active_restrictions(r, "2026-10-31")
    assert "A" not in act["disposal"] and "B" in act["disposal"]
    cur = {"B": 0.05, "D": 0.03, "E": 0.04}
    tgt = {"A": 0.05, "B": 0.08, "C": 0.055, "D": 0.06, "E": 0.0, "F": 0.05, "G": 0.05}
    out, locked = ca.apply_restrictions(tgt, cur, ca.active_restrictions(r, "2026-10-20"))
    assert "A" not in out and out["B"] == 0.05                             # 處置：未持有移除、已持有不得加碼
    assert "C" not in out and out["D"] == 0.06                             # 注意：未持有不建倉、已持有照常
    assert out["E"] == 0.04 and "F" not in out and locked == {"E"}         # 停牌：持有者鎖定現權重、未持有移除
    assert out["G"] == 0.05


def test_restriction_observations():
    from esun_agent.data.prices import load_panel
    r, _ = rs.collect_restrictions(fetcher(ROUTES), date(2026, 10, 19), UNI)
    panel = load_panel()
    D = panel.dates[-1]
    ev = ob.build_evidence(panel, D, inputs={"restrictions": r})
    obs = ev.by_topic("trading_restriction")
    assert len(obs) == 5 and set(ev.restr_obs) == {"disposal", "attention", "suspended"}
    o = ev.obs(ev.restr_obs["disposal"]["3443"])
    assert "上櫃處置股票" in o["statement"] and o["values"]["period_days"] == 12
    assert ev.source_of(o["obs_id"])[0]["url"] == rs.ENDPOINTS[("tpex", "disposal")]
    assert ev.source_of(o["obs_id"])[0]["authority"] == "tpex" and ev.meta[o["obs_id"]]["ticker"] == "3443"


# ------------------------------------------------------------------ BFI82U、DEXTAUS、聯準會
def test_market_flow_observation_is_tw_market():
    from esun_agent.data.prices import load_panel
    panel = load_panel()
    D = panel.dates[-1]
    flow = {"date": D, "content_as_of": f"{D}T15:00:00+08:00", "url": "https://www.twse.com.tw/rwd/zh/fund/BFI82U?x",
            "foreign": -203.12, "trust": 25.0, "dealer": -15.0, "total": -193.12}
    ev = ob.build_evidence(panel, D, inputs={"market_flow": flow})
    o = ev.obs(ev.flow_obs)
    assert o["topic"] == "tw_market" and ev.by_topic("tw_market")[0]["obs_id"] != ev.flow_obs   # 台股整體仍是第一則
    assert o["values"] == {"foreign_net_100m": -203.12, "trust_net_100m": 25.0, "dealer_net_100m": -15.0,
                           "total_net_100m": -193.12}
    assert "外資及陸資(不含外資自營商) -203.12 億元" in o["statement"] and ev.source_of(o["obs_id"])[0]["authority"] == "twse"


def test_dextaus_change_pct_and_twd_flag():
    m = {"series": {"DEXTAUS": {"name": "美元兌新台幣匯率", "unit": " 台幣／美元", "date": "2026-10-16", "value": 32.0,
                                "prev_date": "2026-10-15", "prev_value": 31.8, "chg": round((32.0 / 31.8 - 1) * 100, 4),
                                "chg_unit": "%", "url": "https://fred.stlouisfed.org/series/DEXTAUS"}}}
    flags = ob.macro_risk_flags(m)
    assert len(flags) == 1 and "台幣單日貶值 0.63% > 0.5%" in flags[0]
    from esun_agent.data.prices import load_panel
    panel = load_panel()
    ev = ob.build_evidence(panel, panel.dates[-1], inputs={"macro": m})
    assert ev.by_topic("macro")[0]["values"] == {"value": 32.0, "chg_pct": pytest.approx(0.6289, abs=1e-4)}
    m["series"]["DEXTAUS"]["chg"] = -0.8                                   # 升值不是風險訊號
    assert ob.macro_risk_flags(m) == []


def test_fed_rss_filters_policy_releases_within_36h():
    items = fed.parse_rss(fxb("fed_press_all.xml"))
    assert len(items) == 4 and fed.is_policy_release(items[0]) and not fed.is_policy_release(items[1])
    assert not fed.is_policy_release(items[2])                            # 會議紀錄不是聲明
    now = datetime(2026, 10, 29, 20, 30, tzinfo=TZ)                       # 台北 10/29 晚上（聲明為台北 10/29 02:00）
    rel, warns = fed.collect_fed(fetcher([("press_all.xml", fxb("fed_press_all.xml"))]), now)
    assert warns == [] and rel == [{"title": "Federal Reserve issues FOMC statement",
                                    "url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261028a.htm",
                                    "content_as_of": "2026-10-29T02:00:00+08:00"}]
    assert fed.collect_fed(fetcher([("press_all.xml", fxb("fed_press_all.xml"))]), datetime(2026, 10, 31, 20, 30, tzinfo=TZ))[0] == []
    assert fed.collect_fed(fetcher([]), now)[0] == [] and "RSS" in fed.collect_fed(fetcher([]), now)[1][0]
    from esun_agent.data.prices import load_panel
    panel = load_panel()
    ev = ob.build_evidence(panel, panel.dates[-1], inputs={"fed": rel})
    o = ev.by_topic("macro")[0]
    s = ev.source_of(o["obs_id"])[0]
    assert s["authority"] == "other" and s["url"].endswith("monetary20261028a.htm") and o["values"] == {"n_releases": 1}


# ------------------------------------------------------------------ 組裝器整合
FLOW = {"foreign": -203.12, "trust": 25.0, "dealer": -15.0, "total": -193.12}


def flow_for(D):
    return {"date": D, "content_as_of": f"{D}T15:00:00+08:00", "url": f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?dayDate={D}", **FLOW}


def test_market_view_must_cite_market_flow(env, tmp_path):  # noqa: F811
    sim = ConvSim(env, tmp_path)
    D = env["panel"].dates[env["panel"].dates.index(DAYS[0]) - 1]

    def skip_flow(system, user):
        d = json.loads(conviction_llm()(system, user))
        flow_id = [o["obs_id"] for o in obs_in_prompt(user) if o["topic"] == "tw_market"][-1]
        d["market_view"]["basis_refs"] = [r for r in d["market_view"]["basis_refs"] if r != flow_id]
        return json.dumps(d, ensure_ascii=False)
    r = sim.day(DAYS[0], FakeLLM([skip_flow, conviction_llm()]), extra_inputs={"market_flow": flow_for(D)})
    assert len(r.attempts) == 2 and any("必須引用全市場三大法人買賣金額" in e for e in r.attempts[0]["errors"])
    assert "全市場三大法人買賣金額見" in r.attempts[0]["user"]
    obs = {o["obs_id"]: o for o in r.doc["observations"]}
    assert any("億元" in obs[x]["statement"] for x in r.doc["market_view"]["basis_refs"])


def test_restricted_names_blocked_and_suspended_holding_locked(env, tmp_path):  # noqa: F811
    sim = ConvSim(env, tmp_path)
    panel = env["panel"]
    r1 = sim.day(DAYS[0], FakeLLM([conviction_llm()]))
    held = sorted(sim.ledger.holdings)
    cands = prompt_candidates(r1.attempts[0]["user"])
    picks = set(r1.state.picks)
    susp = next(t for t in held if t in picks and t != "2330")
    disp = next(t for t in held if t in picks and t not in (susp, "2330"))
    new_att = next(t for t in cands if t not in held)
    srcs = [{"authority": a, "kind": k, "url": u} for (a, k), u in rs.ENDPOINTS.items()]
    restr = {"trade_date": DAYS[1], "disposal": {disp: {"authority": "twse"}}, "attention": {new_att: {"authority": "twse"}},
             "suspended": {susp: {"authority": "twse"}}, "sources": srcs}
    before = set(sim.ledger.holdings)
    r2 = sim.day(DAYS[1], FakeLLM([conviction_llm(extra=((new_att, "medium"),)), conviction_llm()]),
                 extra_inputs={"restrictions": restr})
    assert any(f"picks {new_att}：今日為注意股票且未持有" in e for e in r2.attempts[0]["errors"])
    assert new_att not in prompt_candidates(r2.attempts[0]["user"]) and "交易限制（規則 12）" in r2.attempts[0]["user"]
    check_day(sim, r2, DAYS[1], before)
    doc = r2.doc
    assert susp not in {o["ticker"] for o in doc["orders"]}
    nt = next(n for n in doc["no_trade_decisions"] if n["ticker"] == susp)
    assert "暫停交易" in nt["reason"]
    inf = next(i for i in doc["inferences"] if i["inf_id"] == nt["reason_refs"][0])
    obs = {o["obs_id"]: o for o in doc["observations"]}
    assert any(obs[p]["topic"] == "trading_restriction" for p in inf["premise_refs"])
    assert susp in sim.ledger.holdings                                    # 鎖定部位仍在持股
    assert not any(d["ticker"] == disp and d["action"] in ("BUY", "ADD") for d in doc["decisions"])
    assert new_att not in sim.ledger.holdings
    assert any(n.startswith("restrictions:") for n in r2.notes)
