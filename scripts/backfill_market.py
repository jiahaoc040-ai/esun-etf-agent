#!/usr/bin/env python
"""回補歷史日行情（預設 2025-01-01 至今）供回測使用。可中斷續跑（已存在的日期略過）。

  python scripts/backfill_market.py                       # 2025-01-01 → 今天
  python scripts/backfill_market.py --start 2026-01-01 --inst   # 同時補三大法人
註：TWSE 會封高頻 IP，--sleep 預設 3 秒；完整回補約需數小時，建議在本機或 Actions 執行。
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.config import ROOT  # noqa: E402
from esun_agent.data.market import MarketFetcher, backfill  # noqa: E402

RAW_DIR = ROOT / "data" / "raw"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2025, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date.today())
    ap.add_argument("--inst", action="store_true", help="同時回補三大法人買賣超")
    ap.add_argument("--sleep", type=float, default=3.0, help="每次請求前等待秒數")
    ap.add_argument("--force", action="store_true", help="覆寫已存在的日期")
    ap.add_argument("--save-raw", action="store_true", help="把原始回應存到 data/raw/（供製作 fixture）")
    a = ap.parse_args(argv)
    days = backfill(a.start, a.end, MarketFetcher(sleep=a.sleep, raw_dir=RAW_DIR if a.save_raw else None), inst=a.inst, skip_existing=not a.force)
    print(f"完成：新增 {len(days)} 個交易日")
    return 0


if __name__ == "__main__":
    sys.exit(main())
