#!/usr/bin/env python
"""抓公開資訊觀測站法人說明會一覽（150 檔），寫入 config/event_calendar.json 的 stock_events（更新＝改版）。

  python scripts/fetch_conferences.py --start 2026-10-20 --end 2026-11-30 --save-raw
  python scripts/fetch_conferences.py --reclassify          # 分類規則改版後，重新標既有事件（不連網）
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.collect.conference import collect_conferences, reclassify, write_stock_events  # noqa: E402
from esun_agent.collect.http import Fetcher  # noqa: E402
from esun_agent.config import ROOT  # noqa: E402
from esun_agent.strategy.conviction import EVENT_CALENDAR_PATH  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-10-20")
    ap.add_argument("--end", default="2026-11-30")
    ap.add_argument("--save-raw", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只列出，不寫入 event_calendar.json")
    ap.add_argument("--reclassify", action="store_true", help="不抓網路：依目前分類規則重新標 event_calendar 既有法說會的 kind")
    a = ap.parse_args(argv)
    if a.reclassify:
        import json
        cal = json.loads(EVENT_CALENDAR_PATH.read_text(encoding="utf-8"))
        before = {(e["ticker"], e["date"]): e["kind"] for e in cal.get("stock_events", [])}
        cal["stock_events"] = reclassify(cal.get("stock_events", []))
        for e in cal["stock_events"]:
            mark = "  ← 變更" if before.get((e["ticker"], e["date"])) != e["kind"] else ""
            print(f"{e['date']} [{e['kind']}] {e['name']}：{e.get('summary', '')[:40]}{mark}")
        if not a.dry_run:
            EVENT_CALENDAR_PATH.write_text(json.dumps(cal, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            print(f"已寫入 {EVENT_CALENDAR_PATH}")
        return 0
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    f = Fetcher(raw_dir=ROOT / "data" / "raw" / "conference" if a.save_raw else None, sleep=1.0)
    events, warns = collect_conferences(f, start, end)
    for w in warns:
        print("警告：" + w)
    for e in events:
        print(f"{e['date']} [{e['kind']}] {e['name']}：{e['summary'][:40]}")
    n_conf = sum(e["kind"] == "investor_conference" for e in events)
    print(f"共 {len(events)} 場（investor_conference {n_conf}、broker_meeting {len(events) - n_conf}）")
    if not a.dry_run and not (warns and not events):
        write_stock_events(EVENT_CALENDAR_PATH, events, start, end)
        print(f"已寫入 {EVENT_CALENDAR_PATH}")
    return 1 if warns and not events else 0


if __name__ == "__main__":
    raise SystemExit(main())
