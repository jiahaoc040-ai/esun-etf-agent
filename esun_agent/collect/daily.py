"""每日收集：行情／三大法人（D 日）、ETF 前 15 大、MOPS 月營收與重大訊息、台指期夜盤、美股／ADR、FRED 總經數列（macro）。

每一步獨立 try/except：失敗只記警告，降級為缺值（行情與三大法人例外——沒有 D 日行情就不能產生 D-Plan，由
daily_run 判斷）。原始回應全部存到 raw_dir（runs/<交易日>/raw/collect/），供 12/2 審查與製作 fixture。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from ..data import market as mkt
from ..data.market import MarketNotReady
from .http import Fetcher
from .news import collect_news
from .revenue import collect_revenue, fact_event, last_reported_month
from .taifex import collect_taifex
from .fred import collect_macro
from .us import collect_us

ALL_STEPS = ("market", "inst", "etf", "revenue", "news", "taifex", "us", "macro")


@dataclass
class CollectResult:
    D: str
    inputs: dict = field(default_factory=dict)            # {"taifex", "us", "macro", "events"} → DayContext.inputs
    revenue_events: dict[str, dict] = field(default_factory=dict)   # ticker → 月營收事件（daily_run 依焦點名單篩選）
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)          # 不算警告的提示（例如 FRED 的 NDX 晚一天）
    done: list[str] = field(default_factory=list)
    market_ok: bool = False
    market_not_ready: bool = False                        # 最新行情不是 D（當天尚未公布）


def collect_market(D: date, raw_dir: Path | None, inst: bool = True, fetcher: mkt.MarketFetcher | None = None) -> list[str]:
    """抓最新交易日行情並確認就是 D；再抓 D 日三大法人。回傳警告。行情日期不是 D 時丟 RuntimeError。"""
    f = fetcher or mkt.MarketFetcher(sleep=0.5, raw_dir=raw_dir)
    warns: list[str] = []
    df = mkt.fetch_latest(f, expected=D, warnings=warns)
    got = df["date"].iloc[0]
    if got != D.isoformat():
        raise MarketNotReady(f"交易所最新行情日期是 {got}，不是決策日 {D}（收盤資料尚未公布？）")
    mkt.save_daily(df)
    warns += [f"{D} 缺少行情（停牌或遺漏）：{', '.join(m)}" for m in [mkt.check_coverage(df)] if m]
    if inst:
        parts = []
        for label, fn in (("上市 T86", f.twse_inst), ("上櫃三大法人", f.tpex_inst)):
            try:
                parts.append(fn(D))
            except Exception as e:  # noqa: BLE001
                warns.append(f"{label} 抓取失敗：{e}（投信因子以 0 計）")
        if parts:
            mkt.save_daily(pd.concat(parts, ignore_index=True), kind="institutional")
    return warns


def carry_forward_etf(day: str, missing: list[str], base: Path | None = None) -> list[str]:
    """當日抓不到的 ETF 沿用最近一份舊檔的持股（data_dates 保留舊日期，stale 會標出），避免最新檔缺 ETF
    而讓 Active Share 檢查漏掉。回傳成功沿用的代號。"""
    import json
    from ..data.etf_holdings import ETF_TOP15_DIR, top15_path
    base = base or ETF_TOP15_DIR
    p = top15_path(day, base)
    if not p.exists():
        return []
    cur = json.loads(p.read_text(encoding="utf-8"))
    kept = []
    for old in sorted(base.glob("????-??-??.json"), reverse=True):
        if old.stem >= day:
            continue
        prev = json.loads(old.read_text(encoding="utf-8"))
        for c in missing:
            if c not in kept and c in prev.get("top15", {}):
                cur["top15"][c] = prev["top15"][c]
                cur["data_dates"][c] = prev.get("data_dates", {}).get(c, old.stem)
                kept.append(c)
        if len(kept) == len(missing):
            break
    if kept:
        cur.setdefault("carried_forward", {}).update({c: cur["data_dates"][c] for c in kept})
        p.write_text(json.dumps(cur, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return kept


def collect_day(D: date, raw_dir: Path | None, *, steps=ALL_STEPS, now: datetime | None = None,
                do_archive: bool = True, fetcher: Fetcher | None = None) -> CollectResult:
    now = now or datetime.now().astimezone()
    res = CollectResult(D=D.isoformat(), inputs={"events": []})
    f = fetcher or Fetcher(raw_dir=raw_dir, sleep=0.3)

    def step(name, fn):
        if name not in steps:
            return
        try:
            fn()
            res.done.append(name)
        except Exception as e:  # noqa: BLE001 - 每一步獨立
            res.warnings.append(f"[{name}] 失敗：{type(e).__name__}: {e}")

    def do_market():
        try:
            res.warnings.extend(collect_market(D, raw_dir / "market" if raw_dir else None, inst="inst" in steps))
        except MarketNotReady:
            res.market_not_ready = True
            raise
        res.market_ok = True

    def do_etf():
        from ..data.etf_holdings import update
        r = update(D.isoformat(), raw_dir=raw_dir / "etf" if raw_dir else None, sleep=0.5, ref_date=D)
        if r["missing"]:
            kept = carry_forward_etf(D.isoformat(), sorted(r["missing"]))
            res.warnings.append(f"ETF 前 15 大有 {len(r['missing'])} 檔抓不到：{', '.join(sorted(r['missing']))}；"
                                f"沿用前一份資料 {len(kept)} 檔{'' if len(kept) == len(r['missing']) else '（其餘無舊資料，Active Share 檢查會缺這幾檔）'}")
        if r["stale"]:
            res.warnings.append(f"ETF 前 15 大資料落後：{r['stale']}")

    def do_revenue():
        y, m = last_reported_month(D)
        facts, urls, warns = collect_revenue(f, y, m, fetched_at=now)
        res.warnings.extend(warns)
        res.revenue_events = {t: fact_event(x, urls.get(x.market, "")) for t, x in facts.items()}

    def do_news():
        evs, warns = collect_news(f, D)
        res.inputs["events"].extend(evs)
        res.warnings.extend(warns)

    def do_taifex():
        tx, warns = collect_taifex(f, D - timedelta(days=7), now.date())
        res.warnings.extend(warns)
        if tx:
            res.inputs["taifex"] = tx

    def do_us():
        us, warns = collect_us(f, not_after=now.date(), do_archive=do_archive, now=now)
        res.warnings.extend(warns)
        if us:
            res.info.extend(us.get("info", []))
            res.inputs["us"] = us

    def do_macro():
        m, warns = collect_macro(f, not_after=now.date())
        res.warnings.extend(warns)
        if m:
            res.inputs["macro"] = m

    if "market" in steps:
        step("market", do_market)
    for name, fn in (("etf", do_etf), ("revenue", do_revenue), ("news", do_news), ("taifex", do_taifex), ("us", do_us),
                     ("macro", do_macro)):
        step(name, fn)
    return res
