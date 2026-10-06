#!/usr/bin/env python
"""抓 150 檔發行股數 → data/reference/shares_outstanding.csv（回測的市值來源）。

  python scripts/fetch_shares.py --date 2026-10-05
上櫃用 TPEx 日行情的「發行股數」（已驗證）；上市用 TWSE OpenAPI t187ap03_L（格式未驗證）。
抓完請確認涵蓋 150 檔，再重跑 scripts/run_backtest.py。
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.data.market import MarketFetcher  # noqa: E402
from esun_agent.data.shares import fetch_shares, save_shares  # noqa: E402
from esun_agent.config import ROOT  # noqa: E402
from esun_agent.universe import load_universe  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", type=date.fromisoformat, default=date.today())
    ap.add_argument("--save-raw", action="store_true")
    a = ap.parse_args(argv)
    df = fetch_shares(a.date, MarketFetcher(raw_dir=ROOT / "data" / "raw" if a.save_raw else None))
    missing = sorted(set(load_universe()) - set(df["ticker"]))
    p = save_shares(df)
    print(f"寫入 {p}（{len(df)} 檔）")
    if missing:
        print(f"缺 {len(missing)} 檔: {missing}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
