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


def test_collect_corporate_parses_each_endpoint(tmp_path):
    c, warns, info = co.collect_corporate(fetcher(CORP_ROUTES), date(2026, 10, 16), insider_dir=tmp_path / "ins")
    assert warns == [] and {s["kind"] for s in c["sources"]} == set(co.ENDPOINTS)
    assert c["ex_dividend"] == {"2330": {"date": "2026-10-20"}, "2454": {"date": "2026-10-28"}}   # 6 碼權證略過
    assert c["insider"]["2317"] == {"date": "2026-10-16", "shares": 110_000_000.0, "n": 1}         # 自有＋信託
    assert set(c["insider"]) == {"2317", "2882"} and c["insider_days"] == ["2026-10-16"]
    assert (tmp_path / "ins" / "2026-10-16.json").exists()                                         # 依出表日期存檔
    assert info and "累積 1 天" in info[0] and "不足 5" in info[0]
    assert set(c["tv_promo"]) == {"3017"}
    assert c["pledge"] == {"2408": {"ratio_pct": 55.0, "as_of": "2026-08-19"},
                           "6770": {"ratio_pct": 47.81, "as_of": "2026-08-19"}}
    c2, warns, _ = co.collect_corporate(fetcher(CORP_ROUTES[1:]), date(2026, 10, 16), insider_dir=tmp_path / "ins2")
    assert c2["ex_dividend"] == {} and any("除權除息預告抓取失敗" in w for w in warns)


def test_insider_accumulates_daily_files_for_5_trading_days(tmp_path):
    base = tmp_path / "ins"
    for day, f in (("2026-10-15", "corp_insider_20261015.json"), ("2026-10-16", "corp_insider_20261016.json")):
        co.save_insider_day(json.loads(fxb(f)), day, base)
    co.save_insider_day([], "2026-10-08", base)                                   # 超過 5 個交易日，不計
    ins, days = co.load_insider_window("2026-10-16", base)
    assert days == ["2026-10-15", "2026-10-16"] and set(ins) == {"2317", "2882", "2408"}
    assert ins["2408"] == {"date": "2026-10-15", "shares": 3_000_000.0, "n": 1}
    ins, days = co.load_insider_window("2026-10-22", base)                        # 10/16～10/22：10/15 已出窗口
    assert days == ["2026-10-16"] and "2408" not in ins
    # 端點只回當日：第二天的回應不含 10/15 的 2408，但累積檔仍判斷為近 5 日有申報
    routes = [("t187ap12_L", fxb("corp_insider_20261016.json"))]
    c, _, info = co.collect_corporate(fetcher(routes), date(2026, 10, 16), insider_dir=base)
    assert "2408" in c["insider"] and "累積 2 天" in info[0]
    _, down = co.corporate_signals(c, [], "2026-10-16", "2026-10-19")
    assert {"2317", "2882", "2408"} <= set(down)


def test_pledge_text_field_format():
    got = co.parse_corporate(json.loads(fxb("corp_t187ap09_L.json")), "pledge")
    assert got["6770"]["ratio_pct"] == 47.81 and "3040" not in got                # 3040 遠見不在 150 檔
    allu = co.parse_pledge(json.loads(fxb("corp_t187ap09_L.json")), {"3040": {}, "1258": {}, "4148": {}})
    assert allu == {"3040": {"ratio_pct": 99.36, "as_of": "2026-08-19"}, "1258": {"ratio_pct": 92.1, "as_of": "2026-08-19"},
                    "4148": {"ratio_pct": 61.2, "as_of": "2026-08-19"}}               # 名稱含 -KY、比例前只有一個空白
    _, down = co.corporate_signals({"pledge": got}, [], "2026-10-16", "2026-10-19")
    assert "2408" in down and "6770" not in down                                 # 47.81% 未超過 50%


REAL_INSIDER, REAL_PLEDGE = REAL / "corp_insider.json", REAL / "corp_pledge.json"


@pytest.mark.skipif(not REAL_INSIDER.exists(), reason="真實回應待 SS 從 data/raw/corp/ 複製到 tests/fixtures/collect/real/")
def test_real_insider_response():
    recs = json.loads(REAL_INSIDER.read_text(encoding="utf-8"))
    assert recs and all("出表日期" in r and "公司代號" in r for r in recs)
    assert any(k.startswith("預定轉讓總股數") for k in recs[0])
    allu = {r["公司代號"].strip(): {} for r in recs}
    got = co.parse_corporate(recs, "insider", allu)
    assert got and all(x["date"] and x["shares"] > 0 for x in got.values())
    assert co.insider_out_date(recs, "x") != "x"


@pytest.mark.skipif(not REAL_PLEDGE.exists(), reason="真實回應待 SS 從 data/raw/corp/ 複製到 tests/fixtures/collect/real/")
def test_real_pledge_response():
    recs = json.loads(REAL_PLEDGE.read_text(encoding="utf-8"))
    got = co.parse_corporate(recs, "pledge")
    assert got and max(got.values(), key=lambda x: x["ratio_pct"]) == {"ratio_pct": 47.81, "as_of": "2026-08-19"}
    assert max(got, key=lambda t: got[t]["ratio_pct"]) == "6770"


def test_apply_downgrade_and_no_new_restriction():
    lv, hit = ca.apply_downgrade({"A": "high", "B": "medium", "C": "high"}, {"A": [("pledge", "x")], "B": [("insider", "y")]})
    assert lv == {"A": "medium", "B": "medium", "C": "high"} and hit == ["A"]
    out, _ = ca.apply_restrictions({"A": 0.05, "B": 0.05}, {"B": 0.03}, {}, no_new={"A": [], "B": []})
    assert "A" not in out and out["B"] == 0.05


# ------------------------------------------------------------------ 4. 法說會
def test_classify_conference_kind():
    assert cf.classify("受凱基證券邀請參加投資人會議") == "broker_meeting"
    assert cf.classify("2026年第三季財務暨營運報告") == "investor_conference"
    assert cf.classify("本公司 Q3 營運成果") == "investor_conference"
    assert cf.classify("受邀說明第三季營運成果") == "broker_meeting"              # 含「受邀」
    assert cf.classify("參加亞洲科技論壇") == "broker_meeting"


def test_parse_mops_conferences_and_write_calendar(tmp_path):
    rows = cf.parse_t100sb02(fxb("mops_t100sb02_sii_202610.html").decode())
    kinds = {(r["ticker"], r["date"]): r["kind"] for r in rows}
    assert kinds[("2317", "2026-10-20")] == "broker_meeting" and kinds[("2317", "2026-11-12")] == "investor_conference"
    assert kinds[("2303", "2026-10-28")] == "investor_conference"
    assert [k for (t, _), k in kinds.items() if t == "1590"] == ["broker_meeting"] * 5     # 亞德客 5 場皆券商邀約
    assert kinds[("2454", "2026-10-29")] == "investor_conference"                         # 同日兩筆 → 留自辦法說
    assert sum(1 for r in rows if (r["ticker"], r["date"]) == ("2454", "2026-10-29")) == 1
    assert ("9999", "2026-10-30") not in kinds
    with pytest.raises(ValueError, match="公司代號"):
        cf.parse_t100sb02("<table><tr><td>查無資料</td></tr></table>")
    routes = [("ajax_t100sb02_1", fxb("mops_t100sb02_sii_202610.html"))]
    evs, warns = cf.collect_conferences(fetcher(routes), date(2026, 10, 20), date(2026, 11, 30))
    assert warns == [] and len(evs) == len({(e["ticker"], e["date"]) for e in evs})            # 跨月／sii＋otc 重複只留一筆
    conf = {(e["ticker"], e["date"]) for e in evs if e["kind"] == "investor_conference"}
    assert conf == {("2303", "2026-10-28"), ("2454", "2026-10-29"), ("2317", "2026-11-12")}
    assert sum(e["kind"] == "broker_meeting" for e in evs) == 6 and all(e["url"].startswith("https://mopsov") for e in evs)
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"events": [], "stock_events": [{"date": "2026-12-10", "kind": "investor_conference", "ticker": "1101"},
                                                           {"date": "2026-10-21", "kind": "broker_meeting", "ticker": "1590"}]}),
                 encoding="utf-8")
    cal = cf.write_stock_events(p, evs, date(2026, 10, 20), date(2026, 11, 30))
    assert len(cal["stock_events"]) == len(evs) + 1 and cal["stock_events"][-1]["ticker"] == "1101"
    # broker_meeting 不觸發任何限制
    from esun_agent.agent.conviction_agent import conference_events
    confs = conference_events(cal["stock_events"])
    assert {c["ticker"] for c in confs} == {"2303", "2454", "2317", "1101"}
    no_new, _ = co.corporate_signals({}, confs, "2026-10-16", "2026-10-19")
    assert "2317" not in no_new                                                      # 10/20 是券商會議，不限制
    no_new, _ = co.corporate_signals({}, confs, "2026-10-26", "2026-10-27")
    assert {k for k, _ in no_new["2303"]} == {"conference"}                          # 10/28 自辦法說 → 10/27 不新建倉


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


# ------------------------------------------------------------------ 法說會分類：SS 實測 26 場
REAL_CONF = REAL / "conferences_20261020_1130.json"
EXPECT_CONF = {("3105", None), ("2337", None), ("6239", None), ("2303", None), ("3718", None), ("4966", "2026-10-28"),
               ("6510", "2026-10-28"), ("2308", "2026-10-29"), ("2379", None), ("2409", None), ("2454", "2026-10-30"),
               ("2412", None), ("4904", None), ("3529", "2026-11-13"), ("3045", "2026-11-12")}
EXPECT_BROKER = {("2317", "2026-10-20"), ("3529", "2026-10-29"), ("2308", "2026-11-03"), ("2395", None), ("8299", None),
                 ("7750", None)} | {("1590", d) for d in ("2026-11-04", "2026-11-10", "2026-11-17", "2026-11-20", "2026-11-24")}


def _match(expect, t, d):
    return (t, d) in expect or (t, None) in expect


def test_real_26_conferences_classification():
    evs = json.loads(REAL_CONF.read_text(encoding="utf-8"))
    assert len(evs) == 26 and len({(e["ticker"], e["date"]) for e in evs}) == 26
    got = {(e["ticker"], e["date"]): cf.classify(e["summary"]) for e in evs}
    conf = {k for k, v in got.items() if v == "investor_conference"}
    broker = {k for k, v in got.items() if v == "broker_meeting"}
    assert len(conf) == 15 and len(broker) == 11
    assert all(_match(EXPECT_CONF, t, d) for t, d in conf), conf
    assert all(_match(EXPECT_BROKER, t, d) for t, d in broker), broker
    assert sum(t == "1590" for t, _ in broker) == 5
    # 「參加方式」「參加網址」「欲參加者」不構成排除；「受…邀請」要排除
    assert cf.classify("(1)公布本公司115年第3季財務報告。(2)參加方式：請參見網址") == "investor_conference"
    assert cf.classify("擬於10月28日舉行第三季線上法說會，報告本公司營運成果。欲參加者可上網報名") == "investor_conference"
    assert cf.classify("本公司受大和國泰證券邀請，說明本公司115年第2季之財務報告相關資訊") == "broker_meeting"


def test_event_calendar_stock_events_use_current_rules():
    cal = json.loads((ROOT / "config" / "event_calendar.json").read_text(encoding="utf-8"))
    se = cal["stock_events"]
    assert se and all(e["kind"] == cf.classify(e["summary"]) for e in se)               # 已依新規則重新分類
    assert cf.reclassify(se) == se
    assert all(e["name"].endswith("法人說明會" if e["kind"] == "investor_conference" else "投資人會議") for e in se)
    from esun_agent.agent.conviction_agent import conference_events
    assert {c["ticker"] for c in conference_events(se)} >= {"2454", "3045", "4966", "6510"}
