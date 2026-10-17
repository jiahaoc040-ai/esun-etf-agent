#!/usr/bin/env python
"""抓公開資訊觀測站法人說明會一覽（150 檔），寫入 config/event_calendar.json 的 stock_events（更新＝改版）。

  python scripts/fetch_conferences.py --start 2026-10-20 --end 2026-11-30 --save-raw
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.collect.conference import collect_conferences, write_stock_events  # noqa: E402
from esun_agent.collect.http import Fetcher  # noqa: E402
from esun_agent.config import ROOT  # noqa: E402
from esun_agent.strategy.conviction import EVENT_CALENDAR_PATH  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-10-20")
    ap.add_argument("--end", default="2026-11-30")
    ap.add_argument("--save-raw", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只列出，不寫入 event_calendar.json")
    a = ap.parse_args(argv)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    f = Fetcher(raw_dir=ROOT / "data" / "raw" / "conference" if a.save_raw else None, sleep=1.0)
    events, warns = collect_conferences(f, start, end)
    for w in warns:
        print("警告：" + w)
    for e in events:
        print(f"{e['date']} {e['name']}")
    print(f"共 {len(events)} 場")
    if not a.dry_run and not (warns and not events):
        write_stock_events(EVENT_CALENDAR_PATH, events, start, end)
        print(f"已寫入 {EVENT_CALENDAR_PATH}")
    return 1 if warns and not events else 0


if __name__ == "__main__":
    raise SystemExit(main())
