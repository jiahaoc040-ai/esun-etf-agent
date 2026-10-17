"""v2.4.0：CNBC TWD= 匯率、真實處置／注意／停牌回應、上市公司事件、法說會、資料缺失註記、宣告字數。"""
import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from esun_agent.agent import conviction_agent as ca
from esun_agent.agent import observations as ob
from esun_agent.collect import conference as cf, corporate as co, fx, restrictions as rs
from esun_agent.config import ROOT
from esun_agent.strategy.declaration import DECLARATION_PATH, validate_declaration, validate_submission

from .agent_helpers import ConvSim, FakeLLM, conviction_llm, prompt_candidates
from .test_collect import fetcher
from .test_conviction_agent import DAYS, check_day, env  # noqa: F401 - env 為 module fixture

FX = Path(__file__).parent / "fixtures" / "collect"
REAL = FX / "real"
TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 17, 17, 21, tzinfo=TZ)                    # 最新美股交易日 10/16


def fxb(name):
    return (FX / name).read_bytes()


def real(name):
    return json.loads((REAL / name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 1. 匯率：CNBC TWD= 為主、FRED DEXTAUS 備援
DEXTAUS_OLD = {"name": "美元兌新台幣匯率", "unit": " 台幣／美元", "date": "2026-10-09", "value": 32.1, "prev_date": "2026-10-08",
               "prev_value": 32.0, "chg": 0.3125, "chg_unit": "%", "url": "https://fred.stlouisfed.org/series/DEXTAUS"}


def test_parse_cnbc_twd():
    q = fx.parse_cnbc_fx(json.loads(fxb("cnbc_TWD_20261016.json")))
    assert q == {"date": "2026-10-16", "value": 32.429, "change": 0.087, "chg_pct": 0.27}
    no_pct = json.loads(fxb("cnbc_TWD_20261016.json"))
    del no_pct["FormattedQuoteResult"]["FormattedQuote"][0]["change_pct"]
    assert fx.parse_cnbc_fx(no_pct)["chg_pct"] == pytest.approx(0.087 / 32.342 * 100)
    with pytest.raises(ValueError, match="TWD="):
        fx.parse_cnbc_fx({"FormattedQuoteResult": {"FormattedQuote": []}})


def test_fx_prefers_fresh_cnbc_and_falls_back_to_fred():
    f = fetcher([("symbols=TWD%3D", fxb("cnbc_TWD_20261016.json"))])
    x, warns = fx.collect_fx(f, NOW, {"series": {"DEXTAUS": DEXTAUS_OLD}})
    assert warns == [] and x["source"] == "CNBC TWD=" and x["value"] == 32.429 and x["chg"] == 0.27
    assert x["url"] == "https://www.cnbc.com/quotes/TWD=" and x["date"] == "2026-10-16"
    m = fx.merge_fx({"series": {"DEXTAUS": DEXTAUS_OLD, "DGS2": {"x": 1}}, "mode": "api"}, x)
    assert set(m["series"]) == {"DGS2", "USDTWD"}                          # DEXTAUS 不重複出現
    # CNBC 失敗、FRED 新鮮 → 用 FRED
    fresh = dict(DEXTAUS_OLD, date="2026-10-16")
    x, warns = fx.collect_fx(fetcher([]), NOW, {"series": {"DEXTAUS": fresh}})
    assert x["source"] == "FRED DEXTAUS" and warns == []
    # 都舊 → 用最新的並警告
    stale = json.loads(fxb("cnbc_TWD_20261016.json"))
    stale["FormattedQuoteResult"]["FormattedQuote"][0]["last_time"] = "2026-10-14"
    x, warns = fx.collect_fx(fetcher([("TWD%3D", json.dumps(stale).encode())]), NOW, {"series": {"DEXTAUS": DEXTAUS_OLD}})
    assert x["source"] == "CNBC TWD=" and x["date"] == "2026-10-14"
    assert len(warns) == 1 and "所有來源都早於預期 2026-10-16" in warns[0] and "FRED DEXTAUS：最新 2026-10-09" in warns[0]
    x, warns = fx.collect_fx(fetcher([]), NOW, None)
    assert x is None and "全部來源失敗" in warns[0]


def test_fx_observation_and_flag():
    from esun_agent.data.prices import load_panel
    x, _ = fx.collect_fx(fetcher([("TWD%3D", fxb("cnbc_TWD_20261016.json"))]), NOW, None)
    m = fx.merge_fx(None, x)
    panel = load_panel()
    ev = ob.build_evidence(panel, panel.dates[-1], inputs={"macro": m})
    o = ev.by_topic("macro")[0]
    s = ev.source_of(o["obs_id"])[0]
    assert s["url"] == "https://www.cnbc.com/quotes/TWD=" and s["authority"] == "vendor" and "CNBC TWD=" in s["name"]
    assert o["values"] == {"value": 32.429, "chg_pct": 0.27, "chg": 0.087}
    assert "32.429" in o["statement"] and "+0.087" in o["statement"]
    assert ob.macro_risk_flags(m) == []                                    # +0.27% ≤ 0.5%
    m["series"]["USDTWD"]["chg"] = 0.62
    assert "CNBC TWD=" in ob.macro_risk_flags(m)[0]


# ------------------------------------------------------------------ 2. 真實處置／注意／停牌回應
def test_real_twse_punish_fullwidth_tilde_and_4_digit_only():
    recs = real("twse_punish.json")
    assert recs[0]["DispositionPeriod"] == "115/10/15～115/10/21"
    uni = {r["Code"]: {"name": r["Name"], "market": "TWSE"} for r in recs}           # 含 6 碼權證
    got = rs.parse_records(recs, "disposal", date(2026, 10, 19), uni)
    assert got and all(len(t) == 4 for t in got) and "053040" not in got and "911608" not in got
    assert got["2424"]["period"] == ["2026-10-16", "2026-10-22"]
    assert "2492" in got and got["2492"]["period"][1] == "2026-10-19"                    # 迄日當天仍有效
    assert "2492" not in rs.parse_records(recs, "disposal", date(2026, 10, 20), uni)


def test_real_tpex_disposal_compact_period():
    recs = real("tpex_disposal.json")
    assert recs[0]["DispositionPeriod"] == "1151016~1151022"
    uni = {r["SecuritiesCompanyCode"]: {"name": r["CompanyName"], "market": "TPEX"} for r in recs}
    got = rs.parse_records(recs, "disposal", date(2026, 10, 16), uni)
    assert "8111" in got and got["8111"]["period"] == ["2026-10-16", "2026-10-22"]
    assert "81114" not in got and all(len(t) == 4 for t in got)                       # 可轉債 5 碼略過


def test_real_twse_notice_placeholder_and_twtawu_resumed():
    assert rs.parse_records(real("twse_notice.json"), "attention", date(2026, 10, 19)) == {}   # Code 為空字串的佔位資料
    tw = real("twse_twtawu.json")
    uni = {"1218": {"name": "泰山", "market": "TWSE"}}
    assert rs.parse_records(tw, "suspended", date(2026, 10, 19), uni) == {}                     # 已於 8/14 恢復
    assert "1218" in rs.parse_records(tw, "suspended", date(2026, 8, 13), uni)


def test_real_tpex_warning_within_universe():
    got = rs.parse_records(real("tpex_warning.json"), "attention", date(2026, 10, 19))
    assert sorted(got) == ["4979", "6488", "8358"]


# ------------------------------------------------------------------ 3. 上市公司事件
CORP_ROUTES = [("TWT48U_ALL", fxb("corp_twt48u.json")), ("t187ap12_L", fxb("corp_t187ap12_L.json")),
               ("BFZFZU_T", fxb("corp_bfzfzu_t.json")), ("t187ap09_L", fxb("corp_t187ap09_L.json"))]


def test_collect_corporate_parses_each_endpoint():
    c, warns = co.collect_corporate(fetcher(CORP_ROUTES))
    assert warns == [] and {s["kind"] for s in c["sources"]} == set(co.ENDPOINTS)
    assert c["ex_dividend"] == {"2330": {"date": "2026-10-20"}, "2454": {"date": "2026-10-28"}}   # 6 碼權證略過
    assert c["insider"]["2317"] == {"date": "2026-10-16", "shares": 110_000_000.0, "n": 2}
    assert set(c["tv_promo"]) == {"3017"}
    assert c["pledge"]["2408"]["ratio_pct"] == pytest.approx(55.0) and c["pledge"]["2330"]["ratio_pct"] == 0.0
    c2, warns = co.collect_corporate(fetcher(CORP_ROUTES[1:]))
    assert c2["ex_dividend"] == {} and any("除權除息預告抓取失敗" in w for w in warns)


def test_corporate_signals_rules():
    import pandas as pd
    c, _ = co.collect_corporate(fetcher(CORP_ROUTES))
    shares = pd.Series({"2317": 13_900_000_000.0, "2882": 14_600_000_000.0})
    confs = [{"ticker": "2603", "date": "2026-10-20", "kind": "investor_conference"},
             {"ticker": "2454", "date": "2026-10-29", "kind": "investor_conference"}]
    no_new, down = co.corporate_signals(c, confs, "2026-10-16", "2026-10-19", shares=shares)
    assert {k for k, _ in no_new["2330"]} == {"ex_dividend"}            # 10/20 在 T～T+3 內
    assert "2454" not in no_new                                          # 10/28 除息在窗口外、法說 10/29 也不是下一交易日
    assert {k for k, _ in no_new["2317"]} == {"insider"} and "0.79%" in no_new["2317"][0][1]
    assert {k for k, _ in down["2317"]} == {"insider"} and "2882" not in down          # 10/01 申報已超過 5 個交易日
    assert "3017" in no_new and "3017" in down                           # 異常推介：不新建倉＋降級
    assert {k for k, _ in down["2408"]} == {"pledge"} and "2330" not in down
    assert {k for k, _ in no_new["2603"]} == {"conference"}             # 法說 10/20 → 10/19 為前 1 個交易日
    no_new2, _ = co.corporate_signals(c, confs, "2026-10-16", "2026-10-19", shares=None)
    assert "2317" not in no_new2                                         # 沒有發行股數 → 只降級


def test_apply_downgrade_and_no_new_restriction():
    lv, hit = ca.apply_downgrade({"A": "high", "B": "medium", "C": "high"}, {"A": [("pledge", "x")], "B": [("insider", "y")]})
    assert lv == {"A": "medium", "B": "medium", "C": "high"} and hit == ["A"]
    out, _ = ca.apply_restrictions({"A": 0.05, "B": 0.05}, {"B": 0.03}, {}, no_new={"A": [], "B": []})
    assert "A" not in out and out["B"] == 0.05


# ------------------------------------------------------------------ 4. 法說會
def test_parse_mops_conferences_and_write_calendar(tmp_path):
    rows = cf.parse_t100sb02(fxb("mops_t100sb02_sii_202610.html").decode())
    assert [(r["ticker"], r["date"]) for r in rows] == [("2330", "2026-10-16"), ("2454", "2026-10-29"),
                                                       ("2317", "2026-11-12"), ("2603", "2026-12-05")]
    with pytest.raises(ValueError, match="公司代號"):
        cf.parse_t100sb02("<table><tr><td>查無資料</td></tr></table>")
    routes = [("ajax_t100sb02_1", fxb("mops_t100sb02_sii_202610.html"))]
    evs, warns = cf.collect_conferences(fetcher(routes), date(2026, 10, 20), date(2026, 11, 30))
    assert warns == [] and {(e["ticker"], e["date"]) for e in evs} == {("2454", "2026-10-29"), ("2317", "2026-11-12")}
    assert all(e["kind"] == "investor_conference" and e["url"].startswith("https://mopsov.twse.com.tw") for e in evs)
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"events": [], "stock_events": [{"date": "2026-12-10", "kind": "investor_conference", "ticker": "1101"}]}),
                 encoding="utf-8")
    cal = cf.write_stock_events(p, evs, date(2026, 10, 20), date(2026, 11, 30))
    assert [e["ticker"] for e in cal["stock_events"]] == ["2454", "2317", "1101"]


def test_upgrade_news_words():
    assert cf.is_upgrade_news("本公司上修全年營收展望至年增 25%")
    assert cf.is_upgrade_news("董事會通過調升 2026 年財測")
    assert not cf.is_upgrade_news("本公司董事會決議配發現金股利")
    assert not cf.is_upgrade_news("上修")                                  # 需搭配財測／展望等字


# ------------------------------------------------------------------ 5. 資料缺失註記
def test_data_gaps_note():
    missing, have = ca.data_gaps({"us": {"x": 1}, "taifex": {"x": 1}})
    assert "美國利率、VIX 與匯率" in missing and "美股／ADR 收盤" in have
    note = ca.gap_note((missing, have))
    assert note.startswith("今日缺 美國利率、VIX 與匯率") and "判斷僅依 台股行情與個股法人買賣超、美股／ADR 收盤" in note
    full = {k: {"x": 1} for k in ca.GAP_LABELS}
    assert ca.gap_note(ca.data_gaps(full)) is None


# ------------------------------------------------------------------ 組裝器整合
def test_assembler_corporate_signals_gaps_and_upgrade_hint(env, tmp_path):  # noqa: F811
    sim = ConvSim(env, tmp_path)
    r1 = sim.day(DAYS[0], FakeLLM([conviction_llm()]))
    held = sorted(sim.ledger.holdings)
    picks = r1.state.picks
    high = next(t for t, p in picks.items() if p.level == "high" and t != "2330")
    cands = prompt_candidates(r1.attempts[0]["user"])
    blocked = next(t for t in cands if t not in held)
    corp = {"ex_dividend": {blocked: {"date": DAYS[1]}}, "insider": {}, "tv_promo": {},
            "pledge": {high: {"held": 1.0, "pledged": 0.6, "max_ratio": 60.0, "ratio_pct": 60.0}},
            "sources": [{"kind": k, "url": u} for k, u in co.ENDPOINTS.items()]}
    before = set(sim.ledger.holdings)
    r2 = sim.day(DAYS[1], FakeLLM([conviction_llm(extra=((blocked, "medium"),)), conviction_llm()]),
                 extra_inputs={"corporate": corp})
    u = r2.attempts[0]["user"]
    assert any(f"picks {blocked}：今日為" in e and "除權息" in e for e in r2.attempts[0]["errors"])
    assert blocked not in prompt_candidates(u) and "公司事件（規則 13）" in u and "資料缺失（規則 14）" in u
    check_day(sim, r2, DAYS[1], before)
    assert r2.state.picks[high].level == "medium"                        # 董監質押 > 50% → 降一級
    assert any(n.startswith("downgrade:") and high in n for n in r2.notes)
    ce = r2.doc["market_view"]["counter_evidence"]
    assert ce.startswith("今日缺 ") and "判斷僅依 " in ce                   # fixture 沒有 macro 等 → 明寫缺什麼
    assert blocked not in sim.ledger.holdings


def test_upgrade_hint_needs_recent_conference_and_upgrade_news():
    from esun_agent.data.prices import load_panel
    panel = load_panel()
    D = panel.dates[-1]
    news = {"basis": "material_news", "ticker": "2454", "authority": "mops", "url": "https://mops.twse.com.tw/x",
            "content_as_of": f"{D}T17:00:00+08:00", "statement": "聯發科法說會後公告上修第四季營收展望", "values": {"n": 1}}
    ev = ob.build_evidence(panel, D, inputs={"events": [news]})
    confs = [{"ticker": "2454", "date": D, "kind": "investor_conference"}]
    hints = ca.upgrade_hints(ev, confs, D)
    assert list(hints) == ["2454"] and ev.meta[hints["2454"]]["basis"] == "material_news"
    assert ca.upgrade_hints(ev, [], D) == {}


# ------------------------------------------------------------------ 6. 宣告字數
def test_declaration_lengths_by_len():
    d = json.loads(DECLARATION_PATH.read_text(encoding="utf-8"))
    assert d["etf"]["name"] == "主動台灣焦點主題" and len(d["etf"]["name"]) <= 20
    assert d["theme"]["title"] == "以投信籌碼為核心，結合美國事件與散戶關注度，集中布局台股大型股焦點主題"
    assert len(d["theme"]["title"]) <= 50
    assert 100 <= len(d["philosophy_statement"]) <= 280
    assert d["philosophy_statement"].startswith("本基金以「資金流向領先價格」為核心信念")
    assert validate_declaration(d) == [] and d["version"] == "2.4.0"
    bad = copy.deepcopy(d)
    bad["philosophy_statement"] = "理" * 281
    assert any("內部上限 280" in e for e in validate_submission(bad))
    bad["etf"]["name"] = "主動台灣焦點主題2026"
    assert any("中文或英文" in e for e in validate_submission(bad))
