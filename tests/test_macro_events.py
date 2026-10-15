"""v2.3.0：FRED 正式 API（ALFRED 版本）、總經 observation、美國重大事件（前 1 日／當日）、宣告因子。"""
import copy
import json
from datetime import date
from pathlib import Path

import pytest

from esun_agent.agent import observations as ob
from esun_agent.collect import fred, us
from esun_agent.config import ROOT
from esun_agent.strategy import conviction as cv
from esun_agent.strategy.declaration import DECLARATION_PATH, load_declaration, validate_declaration
from tests.test_collect import fetcher

KEY = "testkey0123456789"
PHIL = ("本基金以「資金流向領先價格」為核心信念，以投信買超為首要訊號，搭配外資動向、相對強勢與成交量異動，於台股市值前 150 大中集中配置 10–12 檔高信心標的，"
        "其餘以大型權值股分散風險。同時追蹤美國重大事件，包括聯準會利率決策、通膨與就業數據、科技巨頭財報，以及費城半導體指數與台積電 ADR 表現，"
        "研判其對台灣供應鏈的傳導，於事件前後調整曝險。並以 Google 搜尋熱度、投資社群討論與融資變化衡量散戶關注度，作為辨識題材升溫與過熱的輔助指標。"
        "決策由 AI Agent 依可查證之公開資料逐日推論，刻意降低與主流主動式 ETF 的持股重疊以追求差異化報酬，並嚴守單檔、現金與檔數上限。")


def api_json(*rows):
    return json.dumps({"observations": [{"realtime_start": "2026-10-23", "realtime_end": "2026-10-23", "date": d, "value": v}
                                        for d, v in rows]}).encode()


SERIES = {"DGS2": api_json(("2026-10-21", "3.50"), ("2026-10-22", "3.42"), ("2026-10-23", ".")),
          "DFF": api_json(("2026-10-21", "4.08"), ("2026-10-22", "4.08")),
          "VIXCLS": api_json(("2026-10-22", "22.5"), ("2026-10-23", "26.1")),
          "T10Y2Y": api_json(("2026-10-22", "0.55"), ("2026-10-23", "0.60"))}


def api_routes():
    return [(f"series_id={s}", body) for s, body in SERIES.items()]


# ------------------------------------------------------------------ FRED
def test_fred_api_uses_key_and_alfred_vintage_without_leaking_key(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", KEY)
    f = fetcher(api_routes())
    m, warns = fred.collect_macro(f, not_after=date(2026, 10, 23))
    assert warns == [] and m["mode"] == "api" and set(m["series"]) == {"DGS2", "DFF", "VIXCLS", "T10Y2Y"}
    d = m["series"]["DGS2"]
    assert (d["date"], d["value"], d["prev_date"]) == ("2026-10-22", 3.42, "2026-10-21")      # "." 略過，用實際日期
    assert d["chg"] == pytest.approx(-8.0) and d["chg_unit"] == "bp"
    assert m["series"]["VIXCLS"]["chg"] == pytest.approx(3.6) and m["series"]["VIXCLS"]["chg_unit"] == "點"
    assert d["url"] == "https://fred.stlouisfed.org/series/DGS2"
    calls, kws = f.session.calls, f.session.kw
    assert all(c.startswith(fred.API_URL) for c in calls) and not any(KEY in c for c in calls)
    for kw in kws:
        p = kw["params"]
        assert p["api_key"] == KEY and p["file_type"] == "json"
        assert p["realtime_start"] == p["realtime_end"] == "2026-10-23"                       # ALFRED：當時版本
        assert p["observation_end"] == "2026-10-23"
    assert KEY not in json.dumps(m)
    assert "DTWEXBGS" not in fred.MACRO_SERIES


def test_fred_vintage_for_history_and_failure_warning_has_no_key(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", KEY)
    monkeypatch.setattr("time.sleep", lambda s: None)
    f = fetcher(api_routes())
    fred.fetch(f, "DGS2", end=date(2025, 3, 3), vintage=date(2025, 3, 3))
    assert f.session.kw[0]["params"]["realtime_start"] == "2025-03-03"
    m, warns = fred.collect_macro(fetcher([("series_id=DGS2", ConnectionError("down"))]), not_after=date(2026, 10, 23))
    assert m is None and any("FRED DGS2 失敗" in w for w in warns) and not any(KEY in w for w in warns)


def test_fred_without_key_falls_back_to_fredgraph_csv(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    csv = b"observation_date,DGS2\n2026-10-21,3.50\n2026-10-22,3.42\n"
    f = fetcher([("fredgraph.csv?id=", csv)])
    m, warns = fred.collect_macro(f, not_after=date(2026, 10, 23))
    assert m["mode"] == "csv" and m["series"]["DGS2"]["chg"] == pytest.approx(-8.0)
    assert any("FRED_API_KEY" in w for w in warns)
    assert all("fredgraph.csv" in c for c in f.session.calls)


def test_us_spx_uses_fred_api_when_key_set(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", KEY)
    body = api_json(("2026-10-23", "6533.15"), ("2026-10-26", "6470.80"))
    d, _ = us.collect_us(fetcher([("series_id=SP500", body), ("series_id=NASDAQ100", body), ("yahoo", ConnectionError("x")),
                                  ("nasdaq", ConnectionError("x")), ("stooq", ConnectionError("x"))]), not_after=date(2026, 10, 27))
    assert d["quotes"]["SPX"]["close"] == 6470.80
    assert {s["url"] for s in d["sources"]} == {"https://fred.stlouisfed.org/series/SP500", "https://fred.stlouisfed.org/series/NASDAQ100"}


def test_macro_observations_and_risk_flags(monkeypatch):
    from esun_agent.data.prices import load_panel
    monkeypatch.setenv("FRED_API_KEY", KEY)
    m, _ = fred.collect_macro(fetcher(api_routes()), not_after=date(2026, 10, 23))
    panel = load_panel()
    ev = ob.build_evidence(panel, panel.dates[-1], inputs={"macro": m})
    obs = ev.by_topic("macro")
    assert len(obs) == 4
    dgs2 = next(o for o in obs if "DGS2" in o["statement"])
    assert dgs2["values"] == {"value": 3.42, "chg_bp": -8.0}
    src = ev.source_of(dgs2["obs_id"])[0]
    assert src["authority"] == "vendor" and src["url"] == "https://fred.stlouisfed.org/series/DGS2"
    assert src["content_as_of"] == "2026-10-23T04:00:00+08:00"                 # 觀測日 10/22 美東收盤，不是今天
    vix = next(o for o in obs if "VIXCLS" in o["statement"])
    assert vix["values"] == {"value": 26.1, "chg": 3.6}
    assert all(isinstance(v, (int, float)) for o in obs for v in o["values"].values())
    flags = ob.macro_risk_flags(m)
    assert len(flags) == 1 and "VIX 26.10 > 25" in flags[0]
    m2 = copy.deepcopy(m)
    m2["series"]["DGS2"]["chg"] = -16.0
    m2["series"]["VIXCLS"]["value"] = 25.0
    assert ob.macro_risk_flags(m2) == [f"2 年期殖利率單日 -16.0bp，絕對值 > 15bp（2026-10-22）"]
    assert ob.macro_risk_flags(None) == []


# ------------------------------------------------------------------ 美國事件
def test_us_event_window_day_before_and_event_day():
    ev = [{"date": "2026-11-10", "kind": "cpi", "name": "CPI"}, {"date": "2026-10-28", "kind": "fomc", "name": "FOMC"}]
    assert cv.us_event_window("2026-10-28", ev) == [(ev[1], "當日")]
    assert cv.us_event_window("2026-10-27", ev) == [(ev[1], "前1日")]
    assert cv.us_event_window("2026-10-26", ev) == [] and cv.us_event_window("2026-10-29", ev) == []
    assert cv.us_event_window("2026-11-09", ev) == [(ev[0], "前1日")]
    assert cv.us_event_window("2026-11-06", ev) == []                          # 週五 → 前 1 日是 11/09（週一）
    assert not cv.blackout_events("2026-11-10", ev)                            # CPI 只降低新建倉，不強制凍結
    assert cv.blackout_events("2026-10-28", ev)


def test_event_calendar_official_urls_and_no_guessed_dates():
    raw = json.loads((ROOT / "config" / "event_calendar.json").read_text(encoding="utf-8"))
    official = ("federalreserve.gov", "bls.gov", "bea.gov", "fec.gov", "investor.nvidia.com", "investor.apple.com",
                "microsoft.com", "abc.xyz", "ir.aboutamazon.com", "investor.atmeta.com", "ir.amd.com")
    for e in raw["events"] + raw["to_confirm"]:
        assert e["url"].startswith("https://") and any(d in e["url"] for d in official), e
    assert all("date" not in e for e in raw["to_confirm"])                     # 待確認者不猜日期
    win = [e for e in raw["events"] if raw["window"]["start"] <= e["date"] <= raw["window"]["end"]]
    assert {e["kind"] for e in win} >= {"fomc", "us_election"}
    assert all("confirmed" in e for e in win)
    kinds = {e["kind"] for e in win} | {e["kind"] for e in raw["to_confirm"]}
    assert kinds >= {"fomc", "cpi", "pce", "nfp", "nvidia_earnings", "apple_earnings", "microsoft_earnings",
                     "alphabet_earnings", "amazon_earnings", "meta_earnings", "amd_earnings", "us_election"}
    assert any(e["date"] == "2026-11-03" and e["kind"] == "us_election" for e in win)
    assert all(e.get("date") for e in cv.load_event_calendar())


# ------------------------------------------------------------------ 宣告與提示詞
def test_declaration_v23_name_philosophy_and_factors():
    d = load_declaration()
    assert d["version"] == "2.3.0" and d["etf"]["name"] == "主動台灣焦點主題"
    assert d["philosophy_statement"] == PHIL
    f = {x["id"]: x for x in d["factors"]}
    assert {"us_events", "macro", "retail_attention", "trust_flow"} <= set(f)
    assert f["retail_attention"]["role"] == "auxiliary" and f["retail_attention"]["max_conviction_shift"] == 1
    assert f["retail_attention"]["can_trigger_trade"] is False
    assert f["macro"]["series"] == list(fred.MACRO_SERIES)
    raw = json.loads(DECLARATION_PATH.read_text(encoding="utf-8"))
    bad = copy.deepcopy(raw)
    bad["factors"] = [x for x in bad["factors"] if x["id"] != "macro"]
    assert any("缺 macro" in e for e in validate_declaration(bad))
    bad = copy.deepcopy(raw)
    next(x for x in bad["factors"] if x["id"] == "retail_attention")["can_trigger_trade"] = True
    assert any("retail_attention" in e for e in validate_declaration(bad))
    bad = copy.deepcopy(raw)
    next(x for x in bad["factors"] if x["id"] == "macro")["risk_thresholds"]["vix_max"] = 30.0
    assert any("risk_thresholds" in e for e in validate_declaration(bad))


def test_prompt_and_env_example_updated():
    sys_md = (ROOT / "prompts" / "conviction_system.md").read_text(encoding="utf-8")
    assert "VIX" in sys_md and "> 25" in sys_md and "15bp" in sys_md
    assert "前 1 日與當日" in sys_md and "升或降一級" in sys_md
    user_md = (ROOT / "prompts" / "conviction_user.md").read_text(encoding="utf-8")
    assert "{{us_events}}" in user_md and "{{macro_risk}}" in user_md
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert any(line.startswith("FRED_API_KEY=") and len(line) > len("FRED_API_KEY=") for line in env.splitlines())
