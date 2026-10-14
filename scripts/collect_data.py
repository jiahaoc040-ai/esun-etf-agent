#!/usr/bin/env python
"""手動執行資料收集（daily_run 也用同一套函式）。所有抓取都可加 --save-raw 把原始回應存到 data/raw/collect/。

  python scripts/collect_data.py day --date 2026-10-26 --save-raw            # 行情、法人、ETF、月營收、重大訊息、夜盤、美股
  python scripts/collect_data.py day --steps news,taifex,us --save-raw       # 只抓部分來源（不寫入 data/market）
  python scripts/collect_data.py revenue-backfill --start 2024-09 --save-raw # 月營收歷史（「創新高」需要 24 個月）
  python scripts/collect_data.py holidays --save-raw                         # TWSE 休市日 → data/reference/holidays_<年>.json
"""
import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.collect import calendar as cal  # noqa: E402
from esun_agent.collect.daily import ALL_STEPS, collect_day  # noqa: E402
from esun_agent.collect.http import Fetcher  # noqa: E402
from esun_agent.collect.revenue import collect_revenue, last_reported_month  # noqa: E402
from esun_agent.config import ROOT  # noqa: E402

TZ = timezone(timedelta(hours=8))
RAW = ROOT / "data" / "raw" / "collect"


def months(start: str, end: tuple[int, int]):
    y, m = int(start[:4]), int(start[5:7])
    while (y, m) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("day")
    d.add_argument("--date", default=date.today().isoformat(), help="決策日 D")
    d.add_argument("--steps", default=",".join(ALL_STEPS))
    d.add_argument("--save-raw", action="store_true")
    d.add_argument("--no-archive", action="store_true")
    r = sub.add_parser("revenue-backfill")
    r.add_argument("--start", required=True, help="YYYY-MM")
    r.add_argument("--save-raw", action="store_true")
    r.add_argument("--sleep", type=float, default=3.0, help="每個月份之間等待秒數（MOPS 會擋高頻請求）")
    h = sub.add_parser("holidays")
    h.add_argument("--save-raw", action="store_true")
    a = ap.parse_args(argv)
    now = datetime.now(TZ)

    if a.cmd == "day":
        D = date.fromisoformat(a.date)
        raw = RAW / a.date if a.save_raw else None
        res = collect_day(D, raw, steps=tuple(s.strip() for s in a.steps.split(",") if s.strip()), now=now,
                          do_archive=not a.no_archive)
        print(f"完成：{', '.join(res.done)}；行情 {'OK' if res.market_ok else '—'}；事件 {len(res.inputs['events'])} 則；"
              f"月營收 {len(res.revenue_events)} 檔；夜盤 {'有' if 'taifex' in res.inputs else '無'}；美股 {'有' if 'us' in res.inputs else '無'}")
        print(json.dumps({k: v for k, v in res.inputs.items() if k != "events"}, ensure_ascii=False, indent=1))
        for w in res.warnings:
            print(f"警告：{w}", file=sys.stderr)
        if raw:
            print(f"原始回應：{raw}")
        return 0
    if a.cmd == "revenue-backfill":
        f = Fetcher(raw_dir=RAW / "revenue" if a.save_raw else None)
        n = 0
        for y, m in months(a.start, last_reported_month(now.date())):
            facts, urls, warns = collect_revenue(f, y, m, fetched_at=now)
            n += 1
            print(f"{y}-{m:02d}：{len(facts)} 檔（{', '.join(urls) or '失敗'}）")
            for w in warns:
                print(f"  警告：{w}", file=sys.stderr)
            time.sleep(a.sleep)
        print(f"完成 {n} 個月份 → data/revenue/")
        return 0
    days = cal.fetch_holidays(Fetcher(raw_dir=RAW if a.save_raw else None))
    print(f"休市日 {len(days)} 天：{days[0]} ～ {days[-1]}" if days else "沒有休市日資料")
    return 0


if __name__ == "__main__":
    sys.exit(main())
