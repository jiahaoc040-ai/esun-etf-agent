"""v2.3.0：FRED 正式 API（ALFRED 版本）、總經 observation、美國重大事件（前 1 日／當日）、宣告因子。"""
import copy
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from esun_agent.agent import observations as ob
from esun_agent.collect import fred, us
from esun_agent.config import ROOT
from esun_agent.strategy import conviction as cv
from esun_agent.strategy.declaration import DECLARATION_PATH, load_declaration, validate_declaration
from tests.test_collect import fetcher

KEY = "testkey0123456789"
PHIL = json.loads((ROOT / "config" / "strategy_declaration.json").read_text(encoding="utf-8"))["philosophy_statement"]


def api_json(*rows):
    return json.dumps({"observations": [{"realtime_start": "2026-10-23", "realtime_end": "2026-10-23", "date": d, "value": v}
                                        for d, v in rows]}).encode()


SERIES = {"DGS2": api_json(("2026-10-21", "3.50"), ("2026-10-22", "3.42"), ("2026-10-23", ".")),
          "DFF": api_json(("2026-10-21", "4.08"), ("2026-10-22", "4.08")),
          "VIXCLS": api_json(("2026-10-22", "22.5"), ("2026-10-23", "26.1")),
          "T10Y2Y": api_json(("2026-10-22", "0.55"), ("2026-10-23", "0.60")),
          "DEXTAUS": api_json(("2026-10-15", "31.50"), ("2026-10-16", "31.60"))}


def api_routes():
    return [(f"series_id={s}", body) for s, body in SERIES.items()]


# ------------------------------------------------------------------ FRED
def test_fred_api_live_mode_without_realtime_and_no_key_leak(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", KEY)
    f = fetcher(api_routes())
    m, warns = fred.collect_macro(f, not_after=date(2026, 10, 23))
    assert warns == [] and m["mode"] == "api" and set(m["series"]) == {"DGS2", "DFF", "VIXCLS", "T10Y2Y", "DEXTAUS"}
    d = m["series"]["DGS2"]
    assert (d["date"], d["value"], d["prev_date"]) == ("2026-10-22", 3.42, "2026-10-21")      # "." 略過，用實際日期
    assert d["chg"] == pytest.approx(-8.0) and d["chg_unit"] == "bp"
    assert m["series"]["VIXCLS"]["chg"] == pytest.approx(3.6) and m["series"]["VIXCLS"]["chg_unit"] == "點"
    assert d["url"] == "https://fred.stlouisfed.org/series/DGS2"
    calls, kws = f.session.calls, f.session.kw
    assert all(c.startswith(fred.API_URL) for c in calls) and not any(KEY in c for c in calls)
    for kw in kws:
        p = kw["params"]
        assert p["api_key"] == KEY and p["file_type"] == "json" and p["observation_end"] == "2026-10-23"
        assert "realtime_start" not in p and "realtime_end" not in p                       # 即時：用 FRED 當天版本
    assert KEY not in json.dumps(m)
    assert "DTWEXBGS" not in fred.MACRO_SERIES


def test_fred_taipei_after_midnight_chicago_still_previous_day(monkeypatch):
    """台北 10/17 01:00 = 芝加哥 10/16 11:00：即時模式不帶 realtime；回測模式 vintage 10/17 夾到 10/16。"""
    monkeypatch.setenv("FRED_API_KEY", KEY)
    now = datetime(2026, 10, 17, 1, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    assert fred.fred_today(now) == date(2026, 10, 16)
    f = fetcher(api_routes())
    fred.collect_macro(f, not_after=now.date(), now=now)
    assert f.session.kw and all("realtime_start" not in kw["params"] and "realtime_end" not in kw["params"]
                                for kw in f.session.kw)
    f = fetcher(api_routes())
    fred.collect_macro(f, not_after=now.date(), vintage=date(2026, 10, 17), now=now)
    assert all(kw["params"]["realtime_start"] == kw["params"]["realtime_end"] == "2026-10-16" for kw in f.session.kw)
    f = fetcher(api_routes())
    fred.fetch(f, "DGS2", end=date(2025, 3, 3), vintage=date(2025, 3, 3), now=now)          # 歷史 vintage 不受影響
    assert f.session.kw[0]["params"]["realtime_start"] == "2025-03-03"


class HttpErrorSession:
    """模擬 requests：raise_for_status 的 HTTPError 帶 response，訊息含完整網址（含 api_key）。"""
    def __init__(self, status, body):
        self.status, self.body, self.headers, self.calls = status, body, {}, []

    def get(self, url, params=None, **kw):
        import requests
        self.calls.append(url)
        full = url + "&" + "&".join(f"{k}={v}" for k, v in (params or {}).items())
        sess = self

        class R:
            status_code, content, text = sess.status, sess.body.encode(), sess.body

            def raise_for_status(self):
                raise requests.HTTPError(f"{sess.status} Client Error: Bad Request for url: {full}", response=self)
        return R()


def test_fred_http_error_warning_has_status_and_message_and_masks_key(monkeypatch):
    from esun_agent.collect.http import Fetcher
    monkeypatch.setenv("FRED_API_KEY", KEY)
    body = json.dumps({"error_code": 400, "error_message": "Bad Request.  The value for variable realtime_start is in the future."})
    m, warns = fred.collect_macro(Fetcher(session=HttpErrorSession(400, body), retries=1), not_after=date(2026, 10, 17),
                                  vintage=date(2026, 10, 17))
    assert m is None
    w = next(x for x in warns if "DGS2" in x)
    assert "HTTP 400" in w and "realtime_start is in the future" in w and KEY not in " ".join(warns)
    xml = '<?xml version="1.0"?><error code="400" message="x" error_message="Bad Request.  api_key=abc is not registered."/>'
    _, warns = fred.collect_macro(Fetcher(session=HttpErrorSession(400, xml), retries=1), not_after=date(2026, 10, 17))
    assert any("HTTP 400" in x and "api_key=***" in x for x in warns) and not any("abc" in x or KEY in x for x in warns)
    assert fred.mask_key(f"https://x?series_id=DGS2&api_key={KEY}&file_type=json", KEY) == \
        "https://x?series_id=DGS2&api_key=***&file_type=json"


def test_fred_failure_warning_has_no_key(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", KEY)
    monkeypatch.setattr("time.sleep", lambda s: None)
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
    assert len(obs) == 5
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
    assert d["version"] == "2.4.0" and d["etf"]["name"] == "主動台灣焦點主題"
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
    assert any(line.startswith("FRED_API_KEY=") for line in env.splitlines())          # 金鑰不 commit，由 .env 填
