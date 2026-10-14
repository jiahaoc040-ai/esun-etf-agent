#!/usr/bin/env python
"""檢查／預覽人工收件匣 data/inbox/<交易日>/（daily_run 會用同一個函式讀取）。

  python scripts/ingest_inbox.py 2026-10-27 --prev-date 2026-10-26          # 列出會採用與會略過的檔案
  python scripts/ingest_inbox.py 2026-10-27 --prev-date 2026-10-26 --json   # 輸出 events JSON
預設也會把檔案複製到 runs/<交易日>/raw/inbox/（--no-copy 可關閉）。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.data.inbox import INBOX_DIR, load_inbox  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trade_date")
    ap.add_argument("--prev-date", help="決策日 D；published_at 晚於 D 19:30 的檔案會被略過")
    ap.add_argument("--base", type=Path, default=INBOX_DIR)
    ap.add_argument("--no-copy", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = load_inbox(a.trade_date, a.prev_date, base=a.base, copy_raw=not a.no_copy)
    if a.json:
        print(json.dumps(res.events, ensure_ascii=False, indent=1))
    else:
        print(f"採用 {len(res.accepted)} 份：{'、'.join(res.accepted) or '—'}")
        if res.copied_to:
            print(f"已複製到 {res.copied_to}")
    for w in res.warnings:
        print(f"警告：{w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
