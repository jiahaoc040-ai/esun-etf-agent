"""daily_run 端到端（mock 收集器與 mock LLM，不連網）：連續 3 個交易日 0 ERROR、官方帳務對帳、清晨重產、略過與失敗路徑。"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from esun_agent import daily_run as dr
from esun_agent.agent.llm import LLMError
from esun_agent.collect.daily import CollectResult
from esun_agent.dplan_validate import validate
from esun_agent.official import load_official, official_path

from .agent_helpers import FakeLLM, conviction_llm, load_inputs

TZ = timezone(timedelta(hours=8))
HOL = {"2026-09-25", "2026-09-28"}                      # 讓日曆與 data/market 的交易日一致
NEXT = {"2026-09-24": "2026-09-29", "2026-09-29": "2026-09-30", "2026-09-30": "2026-10-01", "2026-10-01": "2026-10-02"}


def evening(d):
    return datetime.fromisoformat(f"{d}T20:30:00+08:00")


class Collector:
    """回傳 fixture 的夜盤／美股／事件；market_ok=True（data/market 已有資料）。morning 時可指定新的 SOX 變動。"""

    def __init__(self, sox=None, tx=None):
        self.calls, self.sox, self.tx = [], sox, tx

    def __call__(self, D, raw_dir, steps=None, now=None, do_archive=True):
        self.calls.append((D.isoformat(), tuple(steps or ())))
        T = NEXT[D.isoformat()]
        inp = load_inputs(T)
        if steps and set(steps) == {"taifex", "us"}:
            inp = json.loads(json.dumps(inp))
            if self.sox is not None:
                inp["us"]["quotes"]["SOX"]["chg_pct"] = self.sox
                inp["us"]["content_as_of"] = f"{T}T04:00:00+08:00"
            if self.tx is not None:
                inp["taifex"]["chg_pct"] = self.tx
                inp["taifex"]["session_date"] = T
            inp = {k: v for k, v in inp.items() if k in ("us", "taifex")}
        return CollectResult(D=D.isoformat(), inputs=inp, warnings=["測試：收集警告"], done=list(steps or ["all"]),
                             market_ok=True)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_ID", "TEAM_042")
    return dr.Paths(runs=tmp_path / "runs", official=tmp_path / "runs" / "official",
                    ledger=tmp_path / "runs" / "state" / "ledger.json", state=tmp_path / "runs" / "state" / "conviction.json",
                    inbox=tmp_path / "inbox")


def go(paths, d, llm=None, **kw):
    out = []
    r = dr.run(now=kw.pop("now", evening(d)), paths=paths, collector=kw.pop("collector", Collector()), holidays=HOL,
               llm_factory=lambda: llm or FakeLLM([conviction_llm()]), echo=out.append, do_archive=False, **kw)
    return r, "\n".join(map(str, out))


def revalidate(paths, r, D):
    doc = json.loads(r.dplan_path.read_text(encoding="utf-8"))
    from esun_agent.data.prices import load_panel
    close = {t: float(v) for t, v in load_panel().close.loc[D].dropna().items()}
    off = load_official(D, paths.official)
    if off is None:
        led = json.loads(paths.ledger.read_text())
        nav = led["cash"] + sum(n * close[t] for t, n in led["holdings"].items())
        ctx = {"prev_nav": nav, "cash": led["cash"], "holdings": led["holdings"], "prev_close": close}
    else:
        ctx = {"prev_nav": off.nav, "cash": off.cash, "holdings": off.holdings, "prev_close": close}
    return doc, validate(doc, ctx, filename=r.dplan_path.name)


def test_three_trading_days_end_to_end(paths, tmp_path):
    # Day 1（9/28 晚上，週一國定假日 → T=9/29、D=9/24）：沒有官方帳務 → 用自記帳並大字警告
    r1, out1 = go(paths, "2026-09-28")
    assert r1.code == 0 and r1.trade_date == "2026-09-29"
    assert r1.dplan_path == paths.runs / "2026-09-29" / "D-Plan_TEAM_042_2026-09-29.json"
    assert "找不到官方帳務" in out1 and "!!!!" in out1 and "請原封不動上傳此檔" in out1 and str(r1.dplan_path) in out1
    doc1, rep1 = revalidate(paths, r1, "2026-09-24")
    assert rep1.errors == [] and len(doc1["orders"]) >= 21
    raw = paths.raw("2026-09-29")
    assert (raw / "llm" / "attempt_1.json").exists() and (raw / "inputs.json").exists() and (raw / "daily_run.log").exists()
    assert (raw / "state_before.json").exists() and (raw / "summary_evening.txt").exists()
    assert json.loads((paths.day("2026-09-29") / "submissions.json").read_text())["count"] == 1

    # Day 2（9/29 晚上，D=9/29）：先用 new_official_template 產生草稿，模擬「後台」少 1 張、現金差 1 萬 → 對帳以官方為準
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("tmpl", Path(__file__).parent.parent / "scripts" / "new_official_template.py")
    tmpl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tmpl)
    assert tmpl.main(["--date", "2026-09-29", "--official-dir", str(paths.official), "--ledger", str(paths.ledger),
                      "--runs-dir", str(paths.runs)]) == 0
    draft = json.loads(official_path("2026-09-29", paths.official, draft=True).read_text())
    assert draft["status"] == "draft" and draft["team_id"] == "TEAM_042" and len(draft["holdings"]) >= 21
    with pytest.raises(ValueError, match="草稿"):
        from esun_agent.official import parse_official
        parse_official(draft, "2026-09-29")
    off = {k: v for k, v in draft.items() if k not in ("status", "_note")}
    t0 = sorted(off["holdings"])[0]
    off["holdings"][t0] -= 1000
    off["cash"] += 10_000
    official_path("2026-09-29", paths.official).write_text(json.dumps(off), encoding="utf-8")
    r2, out2 = go(paths, "2026-09-29")
    assert r2.code == 0 and "對帳差異" in out2 and t0 in out2 and "以官方為準" in out2
    doc2, rep2 = revalidate(paths, r2, "2026-09-29")
    assert rep2.errors == []
    led = json.loads(paths.ledger.read_text())
    assert led["as_of"] == "2026-09-29" and led["holdings"][t0] == off["holdings"][t0]

    # Day 3（9/30 晚上）：沒有官方檔 → 自記帳（含 Day 2 的委託）推估
    r3, out3 = go(paths, "2026-09-30")
    doc3, rep3 = revalidate(paths, r3, "2026-09-30")
    assert r3.code == 0 and rep3.errors == []
    covered = {d["ticker"] for d in doc3["decisions"]} | {n["ticker"] for n in doc3["no_trade_decisions"]}
    assert set(json.loads(paths.ledger.read_text())["holdings"]) <= covered
    st = json.loads(paths.state.read_text())
    assert st["as_of"] == "2026-10-01" and 10 <= len(st["picks"]) <= 12
    for out in (out1, out2, out3):
        assert "Active Share（前 15 大，最低）" in out and "ERROR 0" in out


def test_morning_regenerates_only_on_big_moves(paths):
    r, _ = go(paths, "2026-09-28")
    first = r.dplan_path.read_text(encoding="utf-8")
    morning = datetime.fromisoformat("2026-09-29T06:50:00+08:00")
    calm, out = go(paths, "2026-09-29", morning=True, now=morning, collector=Collector(sox=-1.2, tx=0.5))
    assert calm.code == 0 and calm.regenerated is False and "無需更新" in out
    assert r.dplan_path.read_text(encoding="utf-8") == first
    col = Collector(sox=-3.4)
    big, out = go(paths, "2026-09-29", morning=True, now=morning, collector=col)
    assert big.regenerated is True and "SOX -3.40%" in out and "請重新上傳" in out
    assert col.calls[0][1] == ("taifex", "us")
    doc = json.loads(big.dplan_path.read_text(encoding="utf-8"))
    assert any("-3.40%" in o["statement"] for o in doc["observations"] if o.get("topic") == "us_overnight") \
        or "-3.4" in json.dumps(doc, ensure_ascii=False)
    assert json.loads((paths.day("2026-09-29") / "submissions.json").read_text())["count"] == 2
    assert list((paths.raw("2026-09-29") / "versions").glob("D-Plan_TEAM_042_2026-09-29.*.json"))
    _, rep = revalidate(paths, big, "2026-09-24")
    assert rep.errors == []
    # 狀態從前一晚「產生前」開始：仍是首次建倉（since = 9/29）
    st = json.loads(paths.state.read_text())
    assert all(p["since"] == "2026-09-29" for p in st["picks"].values())


def test_skip_when_not_eve_of_trading_day_and_holiday_morning(paths):
    r, out = go(paths, "2026-10-02")                      # 週五晚上：下一個交易日是週一
    assert r.code == 0 and r.dplan_path is None and "不是它的前一天" in out
    r, out = go(paths, "2026-09-28", morning=True, now=datetime.fromisoformat("2026-09-28T06:50:00+08:00"))
    assert r.code == 0 and r.regenerated is False and "不是交易日" in out


def test_no_llm_key_still_produces_fallback(paths):
    def boom():
        raise LLMError("缺少 GOOGLE_API_KEY")
    r = dr.run(now=evening("2026-09-28"), paths=paths, collector=Collector(), holidays=HOL, llm_factory=boom,
               echo=None, do_archive=False)
    assert r.code == 0 and any("無法建立 LLM" in w for w in r.warnings) and any("降級" in w for w in r.warnings)
    doc = json.loads(r.dplan_path.read_text(encoding="utf-8"))
    assert doc["agent_metadata"]["model_version"] == "none"


def test_market_failure_without_local_data_aborts(paths):
    class Down(Collector):
        def __call__(self, D, raw_dir, **kw):
            return CollectResult(D=D.isoformat(), inputs={"events": []}, warnings=["[market] 失敗"], market_ok=False)
    r = dr.run(now=evening("2026-10-06"), paths=paths, collector=Down(), holidays=HOL, echo=None, do_archive=False,
               llm_factory=lambda: FakeLLM([conviction_llm()]))
    assert r.code == 1 and r.dplan_path is None and "行情抓取失敗" in r.errors[0]
    assert not list(paths.day("2026-10-07").glob("D-Plan_*.json"))


def test_team_id_missing_uses_tbd(paths, monkeypatch):
    monkeypatch.delenv("TEAM_ID")
    r, out = go(paths, "2026-09-28")
    assert r.dplan_path.name == "D-Plan_TEAM_TBD_2026-09-29.json" and "TEAM_ID 未設定" in out


def test_bad_official_file_falls_back_with_warning(paths):
    official_path("2026-09-24", paths.official).parent.mkdir(parents=True, exist_ok=True)
    official_path("2026-09-24", paths.official).write_text('{"team_id": "TEAM_042", "nav": 1e9}', encoding="utf-8")
    r, out = go(paths, "2026-09-28")
    assert r.code == 0 and any("格式錯誤" in w and "缺少" in w for w in r.warnings)


def test_rerun_same_evening_starts_from_saved_state(paths):
    r1, _ = go(paths, "2026-09-28")
    r2, out = go(paths, "2026-09-28")
    assert r2.code == 0 and "已產生過" in out
    st = json.loads(paths.state.read_text())
    assert all(p["since"] == "2026-09-29" for p in st["picks"].values())
    assert json.loads((paths.day("2026-09-29") / "submissions.json").read_text())["count"] == 2


def test_launchd_plists():
    import plistlib
    from pathlib import Path
    d = Path(__file__).parent.parent / "scripts" / "launchd"
    ev = plistlib.loads((d / "tw.esun.dplan.evening.plist").read_bytes())
    mo = plistlib.loads((d / "tw.esun.dplan.morning.plist").read_bytes())
    assert sorted(x["Weekday"] for x in ev["StartCalendarInterval"]) == [0, 1, 2, 3, 4]        # 週日～週四（交易日前一晚）
    assert {(x["Hour"], x["Minute"]) for x in ev["StartCalendarInterval"]} == {(20, 30)}
    assert sorted(x["Weekday"] for x in mo["StartCalendarInterval"]) == [1, 2, 3, 4, 5]
    assert {(x["Hour"], x["Minute"]) for x in mo["StartCalendarInterval"]} == {(6, 50)}
    assert ev["ProgramArguments"][-1].endswith("scripts/daily_run.py") and mo["ProgramArguments"][-1] == "--morning"
    assert ev["EnvironmentVariables"]["TZ"] == "Asia/Taipei"
    for f in ("install.sh", "uninstall.sh"):
        assert "launchctl" in (d / f).read_text()
