#!/usr/bin/env python
"""抓主動型 ETF 前 10 大持股 → data/etf_top10/YYYY-MM-DD.json。

前置：在 data/reference/etf_sources.csv 填各 ETF 的 format（html/json/csv）與 url。
  python scripts/fetch_etf_holdings.py --save-raw        # 原始頁面存到 data/raw/etf/
抓不到的 ETF 會列出，並產生 data/etf_top10/manual_template.csv；填好後存成
data/etf_top10/manual.csv 再跑一次即可合併。
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.config import ROOT  # noqa: E402
from esun_agent.data.etf_holdings import update  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=date.today().isoformat(), help="資料日期標籤（預設今天）")
    ap.add_argument("--save-raw", action="store_true", help="原始回應存到 data/raw/etf/（供製作 fixture）")
    a = ap.parse_args(argv)
    res = update(a.date, raw_dir=ROOT / "data" / "raw" / "etf" if a.save_raw else None)
    print(f"已存 {len(res['top10'])} 檔；海外型跳過 {len(res['overseas'])} 檔；手動補 {len(res['manual'])} 檔")
    if res["missing"]:
        print(f"\n抓不到 {len(res['missing'])} 檔：")
        for c, why in res["missing"].items():
            print(f"  {c}: {why}")
        print(f"\n請填 {res['template']} 後另存為 data/etf_top10/manual.csv，再重跑。")
    return 1 if res["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
