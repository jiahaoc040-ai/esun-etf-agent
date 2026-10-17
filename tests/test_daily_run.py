"""daily_run 端到端（mock 收集器與 mock LLM，不連網）：連續 3 個交易日 0 ERROR、資料包（DataPack）對帳、清晨重產、
略過與失敗路徑。資料包一律放在暫存資料夾（conftest 已把 ~/Downloads 與 data/datapack 指到空資料夾）。"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from esun_agent import daily_run as dr
from esun_agent.agent.llm import LLMError
from esun_agent.collect.daily import CollectResult
from esun_agent.dplan_validate import validate
from esun_agent.official import load_ledger, project_to

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
        if steps and set(steps) == {"taifex", "us", "macro", "fed"}:
            inp = json.loads(json.dumps(inp))
            if self.sox not in (None, "missing"):
                inp["us"]["quotes"]["SOX"]["chg_pct"] = self.sox
                inp["us"]["content_as_of"] = f"{T}T04:00:00+08:00"
            if self.sox == "missing":
                inp.pop("us")
            if self.tx is not None:
                inp["taifex"]["chg_pct"] = self.tx
                inp["taifex"]["session_date"] = T
            inp = {k: v for k, v in inp.items() if k in ("us", "taifex", "macro")}
        return CollectResult(D=D.isoformat(), inputs=inp, warnings=["測試：收集警告"], done=list(steps or ["all"]),
                             market_ok=True)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_ID", "TEAM_042")
    return dr.Paths(runs=tmp_path / "runs", ledger=tmp_path / "runs" / "state" / "ledger.json",
                    state=tmp_path / "runs" / "state" / "conviction.json", inbox=tmp_path / "inbox",
                    datapack=tmp_path / "datapack", downloads=tmp_path / "Downloads")


def _px(D):
    from esun_agent.data.prices import load_panel
    p = load_panel()
    return ({t: float(v) for t, v in p.avg_price.loc[D].dropna().items()},
            {t: float(v) for t, v in p.close.loc[D].dropna().items()})


def make_datapack(T, D, hold, cash, *, team="TEAM_042", as_of=None, generated_at=None, **over):
    """依持股與現金組一份資料包（close_price 用 D 日交易所收盤）。"""
    _, close = _px(D) if D else ({}, {})
    nav = cash + sum(n * close[t] for t, n in hold.items())
    dp = {"schema_version": "3.0", "doc_type": "DataPack", "team_id": team, "trade_date": T,
          "as_of_date": D if as_of is None else as_of, "generated_at": generated_at or f"{D or T}T18:58:18+08:00",
          "missing": [], "prev_day_orders": {"outcome": "executed", "void_reasons": [], "excluded_codes": []},
          "account": {"cash": cash, "cash_ratio_tradable": cash / nav, "nav_tradable": nav, "aum": nav,
                      "receivable_cash": 0.0, "holdings_count": len(hold)},
          "holdings": [{"code": t, "shares": n, "receivable_shares": 0, "close_price": close[t],
                        "tradable_weight": round(n * close[t] / nav, 6)} for t, n in sorted(hold.items())],
          "compliance": {"violations": 0, "notices": 0, "incidents": []}}
    dp.update(over)
    return dp


def estimated_datapack(paths, T, D, **over):
    """自記帳＋D 日 D-Plan 成交推估出「後台結算」→ 資料包（模擬主辦方 19:00 產生的檔）。"""
    avg, close = _px(D)
    est, _, _ = project_to(D, load_ledger(paths.ledger), avg, close, "TEAM_042", paths.runs)
    return make_datapack(T, D, dict(est["holdings"]), est["cash"], **over)


def put(paths, dp, name="datapack.json", where="downloads"):
    d = getattr(paths, where)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(dp, ensure_ascii=False), encoding="utf-8")
    return d / name


def go(paths, d, llm=None, **kw):
    out = []
    now = kw.pop("now", None) or evening(d)
    r = dr.run(now=now, paths=paths, collector=kw.pop("collector", Collector()), holidays=HOL,
               llm_factory=lambda: llm or FakeLLM([conviction_llm()]), echo=out.append, do_archive=False, **kw)
    return r, "\n".join(map(str, out))


def revalidate(paths, r, D):
    doc = json.loads(r.dplan_path.read_text(encoding="utf-8"))
    _, close = _px(D)
    dpf = r.dplan_path.parent / "raw" / "datapack.json"
    if dpf.exists():
        from esun_agent.datapack import load_datapack
        dp = load_datapack(dpf)
        ctx = {"prev_nav": dp.nav, "cash": dp.cash, "holdings": dp.holdings, "prev_close": close}
    else:
        led = json.loads(paths.ledger.read_text())
        nav = led["cash"] + sum(n * close[t] for t, n in led["holdings"].items())
        ctx = {"prev_nav": nav, "cash": led["cash"], "holdings": led["holdings"], "prev_close": close}
    return doc, validate(doc, ctx, filename=r.dplan_path.name)


def test_three_trading_days_end_to_end(paths, tmp_path):
    # Day 1（9/28 晚上，週一國定假日 → T=9/29、D=9/24）：沒有資料包 → 用自記帳並大字警告「不可上傳」
    r1, out1 = go(paths, "2026-09-28")
    assert r1.code == 0 and r1.trade_date == "2026-09-29" and r1.uploadable is False
    assert r1.dplan_path == paths.runs / "2026-09-29" / "D-Plan_TEAM_042_2026-09-29.json"
    assert "找不到交易日 2026-09-29 的資料包" in out1 and "!!!!" in out1 and "不可上傳" in out1 and str(r1.dplan_path) in out1
    assert "請原封不動上傳此檔" not in out1
    doc1, rep1 = revalidate(paths, r1, "2026-09-24")
    assert rep1.errors == [] and len(doc1["orders"]) >= 21 and doc1["schema_version"] == "4.3"
    raw = paths.raw("2026-09-29")
    assert (raw / "llm" / "attempt_1.json").exists() and (raw / "inputs.json").exists() and (raw / "daily_run.log").exists()
    assert (raw / "state_before.json").exists() and (raw / "summary_evening.txt").exists()
    assert json.loads((paths.day("2026-09-29") / "submissions.json").read_text())["count"] == 1

    # Day 2（9/29 晚上，T=9/30、D=9/29）：資料包＝推估結算但「後台」少 1 張、現金差 1 萬 → 逐檔對帳、以資料包為準
    dp = estimated_datapack(paths, "2026-09-30", "2026-09-29")
    t0 = dp["holdings"][0]["code"]
    dp["holdings"][0]["shares"] -= 1000
    dp["account"]["cash"] += 10_000
    dp["account"]["nav_tradable"] += 10_000 - 1000 * dp["holdings"][0]["close_price"]
    from esun_agent.universe import load_universe
    rcv = next(t for t in sorted(load_universe()) if t not in {h["code"] for h in dp["holdings"]})
    dp["holdings"].append({"code": rcv, "shares": 0, "receivable_shares": 9913, "close_price": 2610.0,
                           "tradable_weight": None})                  # 只有應收配股：不可賣、不計權重與檔數
    put(paths, make_datapack("2026-09-30", "2026-09-29", {}, 1e9, generated_at="2026-09-29T18:00:00+08:00"),
        "old.json")                                                      # 同一個 T 較舊的一份：不採用
    put(paths, {"doc_type": "D-Plan", "trade_date": "2026-09-30"}, "not_a_pack.json")
    put(paths, dp, "DataPack_TEAM_042_2026-09-30.json")
    r2, out2 = go(paths, "2026-09-29")
    assert r2.code == 0 and r2.uploadable is True and "對帳差異" in out2 and t0 in out2 and "以資料包為準" in out2
    assert "請原封不動上傳此檔" in out2 and "送出繳交" in out2 and "當日委託" in out2
    assert json.loads((paths.raw("2026-09-30") / "datapack.json").read_text()) == dp
    doc2, rep2 = revalidate(paths, r2, "2026-09-29")
    assert rep2.errors == []
    covered = {d["ticker"] for d in doc2["decisions"]} | {n["ticker"] for n in doc2["no_trade_decisions"]}
    assert rcv not in covered or any(d["ticker"] == rcv and d["action"] == "BUY" for d in doc2["decisions"])
    for o in doc2["orders"]:
        if o["side"] == "SELL":
            assert o["shares"] <= next((h["shares"] for h in dp["holdings"] if h["code"] == o["ticker"]), 0)
    led = json.loads(paths.ledger.read_text())
    assert led["as_of"] == "2026-09-29" and led["holdings"][t0] == dp["holdings"][0]["shares"]
    assert rcv not in led["holdings"]

    # Day 3（9/30 晚上）：沒有資料包 → 自記帳（含 Day 2 的委託）推估，不可上傳
    r3, out3 = go(paths, "2026-09-30")
    doc3, rep3 = revalidate(paths, r3, "2026-09-30")
    assert r3.code == 0 and rep3.errors == [] and r3.uploadable is False
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
    assert col.calls[0][1] == ("taifex", "us", "macro", "fed")
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


def test_market_failure_without_local_data_aborts(paths, tmp_path):
    paths.market = tmp_path / "market"                    # 本機沒有 D 日行情（不看 repo 的 data/market）
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


def test_datapack_not_settled_stops_with_big_message(paths):
    dp = make_datapack("2026-09-29", None, {}, 1e9, as_of=None, missing=["prev_settlement"], account=None, holdings=None,
                       generated_at="2026-09-28T18:58:11+08:00")
    put(paths, dp)
    r, out = go(paths, "2026-09-28")
    assert r.code == 1 and r.dplan_path is None and "結算尚未產生，請稍後重新下載" in r.errors[0]
    assert "!!!!" in out and "不可當成空手" in out
    assert not list(paths.day("2026-09-29").glob("D-Plan_*.json"))
    # account 為 null（missing 沒寫）也一樣停止
    put(paths, {**dp, "missing": []})
    r, _ = go(paths, "2026-09-28")
    assert r.code == 1 and "結算尚未產生" in r.errors[0]


def test_datapack_first_trading_day_is_all_cash(paths):
    dp = make_datapack("2026-09-29", None, {}, 1e9, as_of=None, generated_at="2026-09-29T05:12:08+08:00",
                       prev_day_orders={"outcome": "none", "void_reasons": [], "excluded_codes": []})
    dp["as_of_date"] = None
    put(paths, dp, where="datapack")                                   # data/datapack/ 也會被找
    r, out = go(paths, "2026-09-28")
    assert r.code == 0 and r.uploadable is True and "第一個交易日" in out
    doc, rep = revalidate(paths, r, "2026-09-24")
    assert rep.errors == [] and all(d["action"] == "BUY" for d in doc["decisions"]) and not doc["no_trade_decisions"]


@pytest.mark.parametrize("field,value,msg", [("trade_date", "2026-09-30", None), ("team_id", "TEAM_999", "team_id"),
                                             ("as_of_date", "2026-09-23", "as_of_date")])
def test_datapack_must_match_trade_date_team_and_decision_day(paths, field, value, msg):
    dp = make_datapack("2026-09-29", "2026-09-24", {}, 1e9)
    dp[field] = value
    put(paths, dp)
    r, out = go(paths, "2026-09-28")
    if msg is None:                                                     # trade_date 不同 → 不是本次 T 的資料包：視為沒找到
        assert r.code == 0 and r.uploadable is False and "找不到交易日 2026-09-29 的資料包" in out
    else:
        assert r.code == 1 and r.dplan_path is None and msg in r.errors[0]


def test_datapack_adopts_team_when_team_id_unset(paths, monkeypatch):
    monkeypatch.delenv("TEAM_ID")
    put(paths, make_datapack("2026-09-29", "2026-09-24", {}, 1e9))
    r, out = go(paths, "2026-09-28")
    assert r.code == 0 and r.dplan_path.name == "D-Plan_TEAM_042_2026-09-29.json" and "TEAM_ID=TEAM_042" in out


def test_summary_lists_void_reasons_excluded_codes_and_incidents_on_top(paths):
    inc = [{"date": "2026-09-23", "check_code": "oversell", "status": "fail", "message": "賣出 5,000 股超過庫存 3,000 股"},
           {"date": "2026-09-24", "check_code": "cash_ratio", "status": "warn", "message": "預估現金比 26.4%，超過上限"},
           {"date": "2026-09-24", "check_code": "info", "status": "pass", "message": "不該出現"}]
    put(paths, make_datapack("2026-09-29", "2026-09-24", {}, 1e9,
                             prev_day_orders={"outcome": "voided", "void_reasons": ["insufficient_funds", "compliance_fail"],
                                              "excluded_codes": ["2330", "2317"]},
                             compliance={"violations": 1, "notices": 1, "incidents": inc}))
    r, out = go(paths, "2026-09-28")
    assert r.code == 0
    summ = r.summary
    top = summ[:summ.index("D-Plan 摘要")]
    assert "oversell" in top and "cash_ratio" in top and "不該出現" not in summ and "違規 FAIL" in top
    assert "作廢原因：insufficient_funds" in summ and "compliance_fail" in summ and "被排除未成交：2330, 2317" in summ


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


# ------------------------------------------------------------------ 決策日判斷
def at(s):
    return datetime.fromisoformat(s + "+08:00")


def test_decide_dates_rules():
    hol = HOL
    # 一般晚上：T = 明天、D = 今天
    assert dr.decide_dates(at("2026-09-29T20:30:00"), hol)[:2] == (date(2026, 9, 30), date(2026, 9, 29))
    # 交易日 14:30 前：今天收盤未公布 → D = 前一交易日、T = D 的下一交易日（今天）
    T, D, note = dr.decide_dates(at("2026-09-29T07:30:00"), hol)
    assert (T, D) == (date(2026, 9, 29), date(2026, 9, 24)) and "早於 14:30" in note
    assert dr.decide_dates(at("2026-09-29T14:29:00"), hol)[1] == date(2026, 9, 24)
    assert dr.decide_dates(at("2026-09-29T14:30:00"), hol)[1] == date(2026, 9, 29)
    # 假日：D 是前一交易日，不受 14:30 影響
    assert dr.decide_dates(at("2026-09-28T10:00:00"), hol)[:2] == (date(2026, 9, 29), date(2026, 9, 24))
    # --trade-date：D = T 的前一個交易日（不看現在時間）
    assert dr.decide_dates(at("2026-09-29T07:00:00"), hol, "2026-09-30")[:2] == (date(2026, 9, 30), date(2026, 9, 29))
    assert dr.decide_dates(at("2026-10-01T22:00:00"), hol, "2026-09-29")[:2] == (date(2026, 9, 29), date(2026, 9, 24))


def test_early_morning_run_builds_todays_plan_from_previous_close(paths):
    r, out = go(paths, "", now=at("2026-09-29T07:30:00"))
    assert r.code == 0 and r.trade_date == "2026-09-29" and "D-Plan_TEAM_042_2026-09-29.json" in r.dplan_path.name
    assert "決策日 D=2026-09-24" in out and "早於 14:30" in out


def test_after_deadline_before_close_refuses_without_force(paths):
    r, out = go(paths, "", now=at("2026-09-29T10:00:00"))
    assert r.code == 1 and r.dplan_path is None and "繳交截止" in r.errors[0] and "今晚重跑" in r.errors[0]
    r, out = go(paths, "", now=at("2026-09-29T10:00:00"), force=True)
    assert r.code == 0 and r.trade_date == "2026-09-29"


def test_trade_date_option_uses_previous_trading_day(paths):
    r, out = go(paths, "", now=at("2026-10-01T09:30:00"), trade_date="2026-09-30")
    assert r.code == 0 and "決策日 D=2026-09-29" in out and r.dplan_path.name.endswith("2026-09-30.json")


def test_market_not_ready_message(paths, tmp_path):
    paths.market = tmp_path / "market"
    class NotReady(Collector):
        def __call__(self, D, raw_dir, **kw):
            return CollectResult(D=D.isoformat(), inputs={"events": []}, warnings=["[market] 失敗：MarketNotReady"],
                                 market_ok=False, market_not_ready=True)
    r = dr.run(now=evening("2026-10-06"), paths=paths, collector=NotReady(), holidays=HOL, echo=None, do_archive=False,
               llm_factory=lambda: FakeLLM([conviction_llm()]))
    assert r.code == 1 and "尚未公布" in r.errors[0] and "稍後重跑" in r.errors[0]


def test_morning_without_us_data_uses_taifex_only(paths):
    go(paths, "2026-09-28")
    morning = datetime.fromisoformat("2026-09-29T06:50:00+08:00")
    r, out = go(paths, "2026-09-29", morning=True, now=morning, collector=Collector(sox="missing", tx=-0.4))
    assert r.regenerated is False and "只用台指期夜盤判斷" in out and "無需更新" in out
    r, out = go(paths, "2026-09-29", morning=True, now=morning, collector=Collector(sox="missing", tx=-2.6))
    assert r.regenerated is True and "台指期夜盤 -2.60%" in out and "只用台指期夜盤判斷" in out


def test_warnings_grouped_by_source_one_line_each():
    ws = ["[us] 失敗：RuntimeError: 抓取失敗 https://x\nTraceback (most recent call last):\n  File ...",
          "SPX 全部來源失敗（FRED：HTTP 503；Stooq：No data），缺值",
          "重大訊息（上櫃）t187ap04_O 回應不是 JSON（前 200 字：<!DOCTYPE html> <html lang=\"zh-Hant-TW\">" + "x" * 300 + "）",
          "上櫃日行情 OpenAPI 失敗（ChunkedEncodingError: IncompleteRead），改用 afterTrading/otc?date=2026/10/26",
          "找不到交易日 2026-10-27 的資料包（DataPack）：改用自記帳產生 D-Plan",
          "收件匣 a.md：缺少同名 .meta.json，略過",
          "台指期夜盤抓取失敗：timeout",
          "SPX 全部來源失敗（FRED：HTTP 503；Stooq：No data），缺值",          # 重複
          "怪訊息"]
    g = dr.group_warnings(ws)
    assert list(g) == ["收件匣", "重大訊息", "台指期夜盤", "美股", "帳務／資料包", "行情／法人", "其他"]
    assert len(g["美股"]) == 2 and g["行情／法人"][0].startswith("上櫃日行情")
    lines = dr.format_warnings(ws)
    assert len(lines) == 8 and all("\n" not in x and len(x) <= dr.WARNING_MAX + 20 for x in lines)
    assert lines[0].startswith("  [收件匣] ") and any(x.endswith("…") for x in lines)


def test_summary_prints_grouped_warnings(paths):
    r, out = go(paths, "2026-09-28")
    assert "流程警告（" in out and "依來源" in out and "  [帳務／資料包] 找不到交易日" in out and "  [其他] 測試：收集警告" in out
    tail = out[out.index("流程警告（"):]
    assert all(len(l) <= dr.WARNING_MAX + 20 for l in tail.splitlines()[1:] if l.startswith("  ["))
