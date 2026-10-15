"""每日流程（scripts/daily_run.py 的實作；在 SS 的 Mac 上由 launchd 觸發）。

晚上（預設，20:30）：
  1. 交易日曆（TWSE 休市日）：T = 今天之後的下一個交易日；只在 T 的前一天晚上產生（--force 可略過）；D = 今天以前最近的交易日。
  2. 收集資料（collect.daily.collect_day）＋人工收件匣（data.inbox.load_inbox）；原始回應存 runs/<T>/raw/。
  3. 帳務：自記帳推估到 D 收盤 → 讀主辦方資料包 DataPack（data/datapack/ 或 ~/Downloads/ 中 trade_date=T 的最新檔，
     複製到 runs/<T>/raw/datapack.json）逐檔對帳（差異列出、以資料包為準）。分母 nav_tradable、持股 shares、現金 cash。
     結算尚未產生（missing 含 prev_settlement／account 為 null）→ 停止；找不到資料包 → 用自記帳並大字警告「不可上傳」。
  4. assemble_conviction_day（LLM 只給名單與信心等級）→ validate() → 有 ERROR 不輸出。
  5. 輸出 runs/<T>/D-Plan_<team_id>_<T>.json，LLM 輸入輸出、收件匣、log 存 runs/<T>/raw/，印摘要。
清晨（--morning，06:50）：重抓美股與台指期夜盤；SOX 或夜盤 |變動| > 2% → 以前一晚的資料與狀態重新產生 D-Plan、
  提示重新上傳（每日提交上限 25 次）；否則印「無需更新」。
"""
from __future__ import annotations

import json
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import datapack as dpk
from .agent.assemble import DayContext
from .agent.conviction_agent import DayResult, assemble_conviction_day, write_conviction_dplan
from .agent.conviction_state import STATE_PATH, ConvictionState
from .agent.llm import LLMError, make_client
from .agent.llm_budget import load_dotenv
from .collect import calendar as cal
from .collect.daily import CollectResult, collect_day
from .collect.http import Fetcher
from .config import ROOT, TSMC, passive_soft_limit
from .data.etf_holdings import check_target_weights, load_top15
from .data.inbox import INBOX_DIR, load_inbox, merge_into_inputs
from .data.prices import MARKET_DIR, load_panel
from .official import LEDGER_PATH, TEAM_TBD, load_ledger, project_to, team_id
from .strategy.candidates import screen
from .strategy.conviction import load_event_calendar
from .strategy.factors import compute_factors, factors_on
from .strategy.marketcap import market_cap
from .universe import load_universe

TZ = timezone(timedelta(hours=8))
RUNS_DIR = ROOT / "runs"
MARKET_READY = time(14, 30)      # 當天收盤行情最早可取得的時間（台灣時間）
SUBMIT_DEADLINE = time(8, 55)    # 交易日 T 當天的繳交截止
MORNING_THRESHOLD = 2.0          # SOX 或台指期夜盤 |變動| > 2% → 重新產生
MAX_SUBMISSIONS = 25


@dataclass
class Paths:
    runs: Path = RUNS_DIR
    ledger: Path = LEDGER_PATH
    state: Path = STATE_PATH
    inbox: Path = INBOX_DIR
    market: Path = MARKET_DIR                   # 「本機是否已有 D 日行情」的判斷
    datapack: Path | None = None                # None → data/datapack/
    downloads: Path | None = None               # None → ~/Downloads/

    def datapack_dirs(self) -> list[Path]:
        return [self.datapack or dpk.DATAPACK_DIR, self.downloads or dpk.DOWNLOADS_DIR]

    def day(self, T: str) -> Path:
        return self.runs / T

    def raw(self, T: str) -> Path:
        return self.runs / T / "raw"


@dataclass
class RunResult:
    code: int                                   # 0 成功或無需動作；1 有錯誤（未輸出 D-Plan）
    trade_date: str | None = None
    dplan_path: Path | None = None
    summary: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    regenerated: bool | None = None             # --morning：是否重新產生
    uploadable: bool = False                    # 有當日資料包且 0 ERROR 才可上傳


class StopRun(RuntimeError):
    """資料包顯示不能繼續（結算尚未產生、不是本次 T、不是本隊…）：不輸出 D-Plan。"""


class Log:
    def __init__(self, echo=print):
        self.lines: list[str] = []
        self.echo = echo

    def __call__(self, msg: str = "") -> None:
        stamp = datetime.now(TZ).strftime("%H:%M:%S")
        for ln in str(msg).splitlines() or [""]:
            self.lines.append(f"[{stamp}] {ln}")
        if self.echo:
            self.echo(msg)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(self.lines) + "\n")


class NoLLM:
    """沒有 API key 時的替代品：每次呼叫都失敗 → 組裝器走降級保底（仍會產生合規 D-Plan）。"""
    provider, model = "other", "none"

    def __init__(self, why: str):
        self.why = why

    def generate(self, system, user, **kw):
        raise LLMError(self.why)


def banner(text: str) -> str:
    bar = "!" * 72
    return f"\n{bar}\n!!  {text}\n{bar}\n"


# --------------------------------------------------------------------------- 日曆

def holidays_for(today: date, fetcher: Fetcher | None, warnings: list[str]) -> set[str]:
    years = [today.year, today.year + 1] if today.month == 12 else [today.year]
    got, missing = cal.load_holidays(years)
    if missing and fetcher is not None:
        try:
            cal.fetch_holidays(fetcher)
            got, missing = cal.load_holidays(years)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"TWSE 休市日抓取失敗：{e}")
    if missing:
        warnings.append(f"沒有 {missing} 年的休市日資料，只排除週末（請確認明天真的開市）")
    return got


def decide_dates(now: datetime, hol: set[str], trade_date: str | None = None) -> tuple[date, date, str | None]:
    """回傳 (交易日 T, 決策日 D, 說明或 None)。
    - --trade-date 指定：D = T 的前一個交易日。
    - 否則 T = 今天之後的下一個交易日、D = T 的前一個交易日（通常就是今天）；但若 D 是今天且現在早於 14:30
      （今天收盤行情尚未公布），D 改取前一個交易日，T 隨之改為 D 的下一個交易日（即今天），讓 D 與 T 相鄰——
      否則 D-Plan 會用「不含今天成交」的庫存下單（可能超賣，C13）。"""
    today = now.date()
    if trade_date:
        T = date.fromisoformat(trade_date)
        return T, cal.prev_trading_day(T, hol, inclusive=False), None
    T = cal.next_trading_day(today, hol)
    D = cal.prev_trading_day(T, hol, inclusive=False)
    if D == today and now.time() < MARKET_READY:
        D = cal.prev_trading_day(today, hol, inclusive=False)
        T = cal.next_trading_day(D, hol)
        return T, D, (f"現在 {now:%H:%M} 早於 {MARKET_READY:%H:%M}，今天 {today} 的收盤行情尚未公布：決策日 D 改用前一個交易日 {D}，"
                      f"交易日 T = {T}")
    return T, D, None


# --------------------------------------------------------------------------- 帳務

@dataclass
class Accounts:
    nav: float                                  # nav_tradable
    cash: float
    holdings: dict[str, int]                    # shares（不含應收配股）
    ledger: object                              # 已更新到 D（以資料包為準），尚未存檔
    datapack: dpk.DataPack | None
    team: str


NO_DATAPACK = "不可上傳"


def load_day_datapack(Ts: str, Ds: str, paths: Paths, team: str, raw: Path, log: Log,
                      warnings: list[str]) -> dpk.DataPack | None:
    """找交易日 T 的資料包並複製到 raw/datapack.json；不能用時丟 StopRun；找不到回傳 None。"""
    errs: list[str] = []
    p = dpk.find_datapack(Ts, paths.datapack_dirs(), errs)
    if p is None:
        msg = (f"找不到交易日 {Ts} 的資料包（DataPack）：已在 {', '.join(str(d) for d in paths.datapack_dirs())} 找過。"
               f"改用自記帳產生 D-Plan——此 D-Plan {NO_DATAPACK}！請 19:00 後到後台「文件繳交 → 下載資料包」，"
               "放進 ~/Downloads 後重跑")
        warnings.extend(errs)
        warnings.append(msg)
        log(banner(msg + ("\n!!  " + "\n!!  ".join(errs) if errs else "")))
        return None
    raw.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, raw / "datapack.json")
    log(f"資料包：{p}（已複製到 {raw / 'datapack.json'}）")
    try:
        dp = dpk.load_datapack(p, Ts, None if team == TEAM_TBD else team)
    except dpk.NotSettledError as e:
        raise StopRun(f"{dpk.NOT_SETTLED_MSG}！（{p.name}：{e}）——不產生 D-Plan，不可當成空手") from e
    except dpk.DataPackError as e:
        raise StopRun(f"資料包不能用：{e}（{p}）——不產生 D-Plan") from e
    if dp.as_of_date is not None and dp.as_of_date != Ds:
        raise StopRun(f"資料包 as_of_date {dp.as_of_date} ≠ 決策日 D={Ds}（T={Ts} 的前一交易日）——請確認交易日或重新下載")
    if dp.as_of_date is None:
        log(f"資料包 as_of_date 為 null 且 missing 為空：第一個交易日，全現金（{dp.cash:,.0f}）建倉")
    warnings.extend(dp.warnings)
    return dp


def accounts(Ts: str, Ds: str, panel, paths: Paths, team: str, raw: Path, log: Log, warnings: list[str]) -> Accounts:
    """自記帳推估到 D 收盤，與資料包逐檔對帳（以資料包為準）。沒有資料包時用自記帳（不可上傳）。"""
    avg = {t: float(v) for t, v in panel.avg_price.loc[Ds].dropna().items()}
    close = {t: float(v) for t, v in panel.close.loc[Ds].dropna().items()}
    dp = load_day_datapack(Ts, Ds, paths, team, raw, log, warnings)
    if dp is not None and team == TEAM_TBD:
        warnings.append(f"TEAM_ID 未設定，改用資料包的 team_id {dp.team_id}——請在 .env 設定 TEAM_ID={dp.team_id}")
        team = dp.team_id
    led = load_ledger(paths.ledger)
    est, w, _ = project_to(Ds, led, avg, close, team, paths.runs)
    warnings.extend(w)
    led.cash, led.holdings, led.as_of = est["cash"], dict(est["holdings"]), Ds
    if dp is None:
        return Accounts(est["nav"], est["cash"], dict(est["holdings"]), led, None, team)
    diffs = dpk.reconcile(led.holdings, led.cash, dp)
    if diffs:
        log(f"對帳差異（自記帳 vs 資料包，以資料包為準，共 {len(diffs)} 項）：")
        for d in diffs:
            log(f"  - {d}")
        warnings.append(f"自記帳與資料包有 {len(diffs)} 項差異，已以資料包為準")
    else:
        log("對帳：自記帳與資料包一致")
    if abs(est["nav"] - dp.nav) > 0.001 * dp.nav:
        warnings.append(f"NAV：自記推估 {est['nav']:,.0f} ≠ 資料包 nav_tradable {dp.nav:,.0f}（以資料包為準）")
    bad_px = [f"{t} 交易所 {close[t]:,} ≠ 資料包 {p:,}" for t, p in sorted(dp.close.items())
              if t in dp.holdings and t in close and abs(close[t] - p) > 1e-6 * max(p, 1)]
    if bad_px:
        warnings.append(f"T-1 收盤價與資料包 close_price 不同（委託以交易所收盤為準）：{'；'.join(bad_px[:5])}")
    no_px = sorted(t for t in dp.holdings if t not in close)
    if no_px:
        warnings.append(f"資料包持股 {no_px} 沒有交易所 {Ds} 收盤價，當日無法交易（列為 no_trade）")
    led.overwrite_from_official(dp.holdings, dp.cash)
    return Accounts(dp.nav, dp.cash, dict(dp.holdings), led, dp, team)


# --------------------------------------------------------------------------- 摘要

# (來源標籤, 關鍵字)：依序比對，第一個命中者為該則警告的來源
WARNING_SOURCES = [
    ("收件匣", ("收件匣",)),
    ("重大訊息", ("重大訊息", "[news]")),
    ("月營收", ("月營收", "[revenue]")),
    ("台指期夜盤", ("台指期", "夜盤", "[taifex]")),
    ("美股", ("美股", "SOX", "NDX", "SPX", "TSM", "[us]")),
    ("ETF 持股", ("ETF", "[etf]")),
    ("帳務／資料包", ("資料包", "DataPack", "官方", "自記帳", "對帳", "TEAM_ID", "帳務", "team_id")),
    ("LLM", ("LLM", "降級", "模型")),
    ("行情／法人", ("行情", "T86", "三大法人", "法人", "[market]", "[inst]")),
    ("日曆／提交", ("休市", "交易日", "繳交", "提交", "版本", "D-Plan")),
]
WARNING_MAX = 110


def one_line(msg: str, width: int = WARNING_MAX) -> str:
    s = " ".join(str(msg).split())
    return s if len(s) <= width else s[:width - 1] + "…"


def group_warnings(warnings: list[str]) -> dict[str, list[str]]:
    """依來源分組（保留來源順序），每則縮成一行；完全相同的訊息只留一則。"""
    out: dict[str, list[str]] = {}
    for w in warnings:
        src = next((lab for lab, keys in WARNING_SOURCES if any(k in w for k in keys)), "其他")
        line = one_line(w)
        if line not in out.setdefault(src, []):
            out[src].append(line)
    order = [lab for lab, _ in WARNING_SOURCES] + ["其他"]
    return {k: out[k] for k in order if k in out}


def format_warnings(warnings: list[str]) -> list[str]:
    return [f"  [{src}] {line}" for src, lines in group_warnings(warnings).items() for line in lines]


def summarize(res: DayResult, ctx: DayContext, path: Path | None, warnings: list[str], llm) -> str:
    """dp（ctx.datapack）的合規 warn／fail 印在最上方；沒有資料包時最後一行是「不可上傳」。"""
    doc = res.doc
    dp = ctx.datapack
    nav, close = ctx.prev_nav, ctx.prev_close
    post = dict(ctx.holdings)
    for o in doc["orders"]:
        post[o["ticker"]] = post.get(o["ticker"], 0) + (o["shares"] if o["side"] == "BUY" else -o["shares"])
    post = {t: n for t, n in post.items() if n > 0}
    w = {t: n * close[t] / nav for t, n in post.items() if t in close}
    buy = sum(o["shares"] * close[o["ticker"]] for o in doc["orders"] if o["side"] == "BUY")
    sell = sum(o["shares"] * close[o["ticker"]] for o in doc["orders"] if o["side"] == "SELL")
    cash_after = (ctx.cash - buy + sell) / nav
    names = load_universe()
    picks = res.state.picks
    L = [""]
    alerts = dpk.alert_lines(dp) if dp is not None else []
    if alerts:
        L += ["!" * 72, f"!! 資料包合規紀錄（warn／fail 共 {len(alerts)} 筆；違規 {dp.violations} 天、提示 {dp.notices} 列）"]
        L += [f"!! {a}" for a in alerts] + ["!" * 72]
    L += ["=" * 72, f"D-Plan 摘要｜交易日 {ctx.trade_date}（決策日 {ctx.prev_date}）｜隊伍 {ctx.team_id}", "=" * 72]
    L += dpk.summary_lines(dp) if dp is not None else [f"資料包：無——帳務用自記帳推估（{NO_DATAPACK}）"]
    L += [
         f"模式：{ {'llm': 'Agent 正常產生', 'hold': '降級：凍結既有名單', 'base': '降級：合規基準 A'}[res.mode] }"
         f"｜模型 {doc['agent_metadata']['model_provider']}/{doc['agent_metadata']['model_version']}"
         f"｜LLM 嘗試 {len(res.attempts)} 次｜tokens 入 {res.usage['input']:,} 出 {res.usage['output']:,}"]
    L.append(f"委託後（以前日收盤估算）持股 {len(post)} 檔、現金比 {cash_after:.2%}、委託 {len(doc['orders'])} 筆"
             f"（買 {buy / nav:.1%}、賣 {sell / nav:.1%}）、posture {doc['market_view']['posture']['net_exposure_intent']}")
    if ctx.top15 and w:
        ap = check_target_weights(w, ctx.top15, margin=0.0, expected_etfs=list(ctx.top15))
        L.append(f"Active Share（前 15 大，最低）：{ap['min']:.1%}（{ap['worst_etf']}；官方門檻 15%）")
    L.append("持股（委託後估算權重）：")
    for t, x in sorted(w.items(), key=lambda kv: -kv[1]):
        tag = f"高信心-{'高' if picks[t].level == 'high' else '中'}" if t in picks else "湊檔"
        L.append(f"  {t} {names.get(t, {}).get('name', ''):<6} {x:6.2%}  {tag}{'  ⚠ 接近上限' if x > passive_soft_limit(t) else ''}")
    if doc["orders"]:
        L.append("委託：")
        dec = {d["decision_id"]: d for d in doc["decisions"]}
        for o in doc["orders"]:
            L.append(f"  {o['side']:<4} {o['ticker']} {names.get(o['ticker'], {}).get('name', ''):<6} {o['shares']:>10,} 股"
                     f"（{dec[o['decision_ref']]['action']} → 目標 {dec[o['decision_ref']]['target_weight']:.2%}）")
    else:
        L.append("委託：無（全部 no_trade）")
    rep = res.report
    L.append(f"驗證：ERROR {len(rep.errors)}、WARN {len(rep.warnings)}；流程警告 {len(warnings)}")
    for e in rep.errors:
        L.append(f"  ERROR {e}")
    for x in rep.warnings[:10]:
        L.append(f"  WARN  {x}")
    if warnings:
        L.append(f"流程警告（{len(warnings)} 則，依來源）：")
        L += format_warnings(warnings)
    if path is not None and dp is None:
        L += ["", f"✗✗✗ {NO_DATAPACK}：沒有當日資料包，帳務是自記帳推估（可能超賣而整份被拒）。檔案僅供參考：", f"  {path}",
              "    → 19:00 後到後台下載資料包，放進 ~/Downloads，重跑 python scripts/daily_run.py"]
    elif path is not None:
        L += ["", "★ 請原封不動上傳此檔，上傳後按「送出繳交」，再到「每日數據 → 當日委託」核對：", f"  {path}"]
    L.append("=" * 72)
    return "\n".join(L)


def count_submission(T: str, paths: Paths, mode: str, kind: str) -> int:
    p = paths.day(T) / "submissions.json"
    d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"count": 0, "versions": []}
    d["count"] += 1
    d["versions"].append({"at": datetime.now(TZ).isoformat(timespec="seconds"), "mode": mode, "kind": kind})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d["count"]


def save_attempts(res: DayResult, llm, raw: Path) -> None:
    d = raw / "llm"
    d.mkdir(parents=True, exist_ok=True)
    for a in res.attempts:
        (d / f"attempt_{a['attempt']}.json").write_text(json.dumps(a, ensure_ascii=False, indent=1), encoding="utf-8")
    if getattr(llm, "log", None):
        (d / "calls.json").write_text(json.dumps(llm.log, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- 主流程

def _make_llm(llm_factory, warnings):
    try:
        return (llm_factory or make_client)()
    except LLMError as e:
        warnings.append(f"無法建立 LLM：{e}——今天只能產生降級保底 D-Plan")
        return NoLLM(str(e))


def run(*, now: datetime | None = None, morning: bool = False, force: bool = False, trade_date: str | None = None,
        paths: Paths | None = None, collector=collect_day, llm_factory=None, holidays: set[str] | None = None,
        fetcher: Fetcher | None = None, echo=print, do_archive: bool = True) -> RunResult:
    load_dotenv()
    paths = paths or Paths()
    now = (now or datetime.now(TZ)).astimezone(TZ)
    today = now.date()
    log = Log(echo)
    warnings: list[str] = []
    team = team_id(warnings)
    hol = holidays if holidays is not None else holidays_for(today, fetcher or Fetcher(), warnings)

    if morning:
        T = date.fromisoformat(trade_date) if trade_date else today
        if not cal.is_trading_day(T, hol):
            log(f"今天 {T} 不是交易日，清晨檢查略過")
            return RunResult(0, T.isoformat(), regenerated=False)
        D = cal.prev_trading_day(T, hol, inclusive=False)
    else:
        T, D, note = decide_dates(now, hol, trade_date)
        if note:
            warnings.append(note)
            log(note)
        if (T - today).days > 1 and not force and not trade_date:
            log(f"下一個交易日是 {T}，今晚不是它的前一天；不產生 D-Plan（請在 {T - timedelta(days=1)} 晚上執行，或加 --force）")
            return RunResult(0, T.isoformat())
        deadline = datetime.combine(T, SUBMIT_DEADLINE, tzinfo=TZ)
        if now > deadline:
            if force or trade_date:
                warnings.append(f"交易日 {T} 的繳交截止（{SUBMIT_DEADLINE:%H:%M}）已過，這份 D-Plan 只能做測試／留存")
            else:
                msg = (f"交易日 {T} 的繳交截止（{T} {SUBMIT_DEADLINE:%H:%M}）已過，不產生 D-Plan"
                       + ("（今天收盤行情 14:30 後才公布，請在今晚重跑）" if note else "") + "；測試請加 --force")
                log(banner(msg))
                return RunResult(1, T.isoformat(), errors=[msg], warnings=warnings)
    Ts, Ds = T.isoformat(), D.isoformat()
    raw = paths.raw(Ts)
    raw.mkdir(parents=True, exist_ok=True)
    log(f"== daily_run {'--morning ' if morning else ''}{now:%Y-%m-%d %H:%M}｜交易日 T={Ts}｜決策日 D={Ds}｜隊伍 {team}")
    try:
        if morning:
            return _morning(now, Ts, Ds, paths, raw, collector, llm_factory, log, warnings, team, do_archive)
        return _evening(now, Ts, Ds, paths, raw, collector, llm_factory, log, warnings, team, do_archive)
    except StopRun as e:
        log(banner(str(e)))
        return RunResult(1, Ts, errors=[str(e)], warnings=warnings)
    except Exception as e:  # noqa: BLE001 - 任何未預期錯誤：不輸出 D-Plan，完整記錄
        log(banner(f"daily_run 失敗：{type(e).__name__}: {e}——沒有輸出 D-Plan"))
        log(traceback.format_exc())
        return RunResult(1, Ts, errors=[f"{type(e).__name__}: {e}"], warnings=warnings)
    finally:
        log.save(raw / ("morning.log" if morning else "daily_run.log"))


def _build_and_write(now, Ts, Ds, paths, raw, inputs, state, llm_factory, log, warnings, team, kind):
    panel = load_panel()
    if Ds not in panel.dates:
        raise RuntimeError(f"行情資料沒有決策日 {Ds}（data/market/{Ds}.parquet）——無法產生 D-Plan")
    factors = compute_factors(panel)
    caps = market_cap(panel)[0]
    top15 = load_top15()
    acc = accounts(Ts, Ds, panel, paths, team, raw, log, warnings)
    led = acc.ledger
    llm = _make_llm(llm_factory, warnings)
    ctx = DayContext(team_id=acc.team, trade_date=Ts, prev_date=Ds, panel=panel, factors=factors, caps=caps, top15=top15,
                     prev_nav=acc.nav, cash=acc.cash, holdings=acc.holdings, inputs=inputs, fetched_at=now,
                     datapack=acc.datapack)
    log(f"組裝 D-Plan（LLM {getattr(llm, 'provider', '?')}/{getattr(llm, 'model', '?')}）…")
    res = assemble_conviction_day(ctx, llm, state, events=load_event_calendar())
    save_attempts(res, llm, raw)
    (raw / "inputs.json").write_text(json.dumps(inputs, ensure_ascii=False, indent=1), encoding="utf-8")
    led.save(paths.ledger)
    if not res.report.ok:
        summary = summarize(res, ctx, None, warnings, llm)
        log(summary)
        log(banner("D-Plan 有 ERROR，不輸出（請看上方 ERROR 與 runs/<T>/raw/）"))
        return RunResult(1, Ts, None, summary, warnings, list(res.report.errors))
    out = paths.day(Ts)
    old = sorted(out.glob("D-Plan_*.json"))
    if old:
        vdir = raw / "versions"
        vdir.mkdir(parents=True, exist_ok=True)
        for p in old:
            shutil.copy2(p, vdir / f"{p.stem}.{datetime.now(TZ):%H%M%S}.json")
            if p.name != res.filename:
                p.unlink()
    path = write_conviction_dplan(res, out, paths.state)
    n = count_submission(Ts, paths, res.mode, kind)
    if n >= MAX_SUBMISSIONS:
        warnings.append(f"今天已產生 {n} 個版本，主辦方每日提交上限 {MAX_SUBMISSIONS} 次——不要再重產")
    if res.mode != "llm":
        warnings.append(f"今日為降級 D-Plan（{res.mode}）：LLM 嘗試 {len(res.attempts)} 次都未通過，原因見 raw/llm/")
    summary = summarize(res, ctx, path, warnings, llm)
    (raw / f"summary_{kind}.txt").write_text(summary, encoding="utf-8")
    log(summary)
    if acc.datapack is None:
        log(banner(f"沒有資料包：這份 D-Plan {NO_DATAPACK}（下載資料包放進 ~/Downloads 後重跑）"))
    return RunResult(0, Ts, path, summary, warnings, [], uploadable=acc.datapack is not None)


def _evening(now, Ts, Ds, paths, raw, collector, llm_factory, log, warnings, team, do_archive):
    D = date.fromisoformat(Ds)
    log("收集資料…")
    col: CollectResult = collector(D, raw / "collect", now=now, do_archive=do_archive)
    warnings.extend(col.warnings)
    log(f"  完成：{', '.join(col.done) or '（無）'}；警告 {len(col.warnings)} 則")
    if not col.market_ok and not (paths.market / f"{Ds}.parquet").exists():
        if col.market_not_ready:
            msg = (f"{Ds} 的收盤行情尚未公布（交易所最新資料仍是前一交易日）——不產生 D-Plan；請稍後重跑"
                   f"（決策日會一直是 {Ds}：若改用前一交易日，下單會用不含今天成交的庫存而可能超賣）")
        else:
            msg = f"{Ds} 行情抓取失敗且本機沒有資料——無法產生 D-Plan"
        log(banner(msg))
        return RunResult(1, Ts, errors=[msg], warnings=warnings)
    ib = load_inbox(Ts, Ds, base=paths.inbox, runs_dir=paths.runs)
    warnings.extend(ib.warnings)
    log(f"收件匣：採用 {len(ib.accepted)} 份、略過 {len(ib.skipped)} 份")
    inputs = merge_into_inputs(col.inputs, ib)
    state = ConvictionState.load(paths.state)
    if state.as_of == Ts and (raw / "state_before.json").exists():      # 同一個 T 重跑：從產生前的狀態開始
        state = ConvictionState.load(raw / "state_before.json")
        warnings.append(f"交易日 {Ts} 的 D-Plan 已產生過，這次重跑會覆蓋（舊版存於 raw/versions/）")
    else:
        state.save(raw / "state_before.json")
    if col.revenue_events:
        inputs["events"] += _focus_revenue(col.revenue_events, Ds, state, paths, warnings)
    return _build_and_write(now, Ts, Ds, paths, raw, inputs, state, llm_factory, log, warnings, team, "evening")


def _focus_revenue(rev: dict[str, dict], Ds: str, state: ConvictionState, paths: Paths, warnings: list[str]) -> list[dict]:
    """月營收事件只給「候選池 ∪ 持股 ∪ 高信心名單」，控制提示詞長度。"""
    try:
        panel = load_panel()
        fdf = factors_on(compute_factors(panel), Ds)
        focus = set(screen(fdf, load_top15(), held=set(state.picks)).index)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"月營收焦點名單計算失敗（{e}），只給高信心名單的營收")
        focus = set()
    focus |= set(state.picks) | set(load_ledger(paths.ledger).holdings)
    return [ev for t, ev in sorted(rev.items()) if t in focus]


def _morning(now, Ts, Ds, paths, raw, collector, llm_factory, log, warnings, team, do_archive):
    D = date.fromisoformat(Ds)
    prev_inputs = raw / "inputs.json"
    if not prev_inputs.exists() or not (raw / "state_before.json").exists():
        warnings.append("找不到前一晚的輸入（runs/<T>/raw/inputs.json），改跑完整流程")
        log(banner("找不到前一晚的 D-Plan 輸入，改跑完整流程（請確認昨晚 daily_run 是否成功）"))
        return _evening(now, Ts, Ds, paths, raw, collector, llm_factory, log, warnings, team, do_archive)
    col = collector(D, raw / "collect_morning", steps=("taifex", "us"), now=now, do_archive=do_archive)
    warnings.extend(col.warnings)
    inputs = json.loads(prev_inputs.read_text(encoding="utf-8"))
    moves = []
    us_new, tx_new = col.inputs.get("us"), col.inputs.get("taifex")
    if not us_new:
        log("美股資料仍缺（所有來源失敗）：只用台指期夜盤判斷是否重產")
    if not tx_new:
        log("台指期夜盤資料缺值：只用美股 SOX 判斷是否重產")
    if us_new and us_new.get("content_as_of") != (inputs.get("us") or {}).get("content_as_of"):
        sox = us_new["quotes"].get("SOX", {}).get("chg_pct")
        if sox is not None and abs(sox) > MORNING_THRESHOLD:
            moves.append(f"SOX {sox:+.2f}%")
    if tx_new and tx_new.get("session_date") != (inputs.get("taifex") or {}).get("session_date"):
        if abs(tx_new["chg_pct"]) > MORNING_THRESHOLD:
            moves.append(f"台指期夜盤 {tx_new['chg_pct']:+.2f}%")
    if not moves:
        sox = (us_new or {}).get("quotes", {}).get("SOX", {}).get("chg_pct")
        tx = (tx_new or {}).get("chg_pct")
        log(f"無需更新（SOX {'—' if sox is None else f'{sox:+.2f}%'}、台指期夜盤 {'—' if tx is None else f'{tx:+.2f}%'}，"
            f"門檻 ±{MORNING_THRESHOLD:.0f}%）；昨晚的 D-Plan 維持不變")
        return RunResult(0, Ts, None, "無需更新", warnings, regenerated=False)
    sub = paths.day(Ts) / "submissions.json"
    used = json.loads(sub.read_text(encoding="utf-8"))["count"] if sub.exists() else 0
    if used >= MAX_SUBMISSIONS:
        msg = f"今天已產生 {used} 個版本，達每日提交上限 {MAX_SUBMISSIONS}，不再重產"
        log(banner(msg))
        return RunResult(0, Ts, None, msg, warnings + [msg], regenerated=False)
    log(f"隔夜大幅變動：{'、'.join(moves)} → 以最新美股與夜盤重新產生 D-Plan")
    if us_new:
        inputs["us"] = us_new
    if tx_new:
        inputs["taifex"] = tx_new
    state = ConvictionState.load(raw / "state_before.json")
    r = _build_and_write(now, Ts, Ds, paths, raw, inputs, state, llm_factory, log, warnings, team, "morning")
    r.regenerated = r.code == 0
    if r.code == 0:
        log(banner(f"D-Plan 已重新產生（{'、'.join(moves)}）——請重新上傳：{r.dplan_path}"))
    return r


def main_cli(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--morning", action="store_true", help="清晨模式：重抓美股與夜盤，大幅變動才重產")
    ap.add_argument("--force", action="store_true", help="今晚不是下一個交易日的前一天也照樣產生")
    ap.add_argument("--trade-date", help="指定交易日 T（YYYY-MM-DD）")
    ap.add_argument("--no-archive", action="store_true", help="不送 web.archive.org 存檔（較快）")
    a = ap.parse_args(argv)
    r = run(morning=a.morning, force=a.force, trade_date=a.trade_date, do_archive=not a.no_archive)
    return r.code


if __name__ == "__main__":
    sys.exit(main_cli())
