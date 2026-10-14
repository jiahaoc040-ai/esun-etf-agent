#!/usr/bin/env python
"""依前一日帳務推估「結算日 D」的官方帳務草稿 → runs/official/<D>.draft.json。

  python scripts/new_official_template.py              # D = data/market 最新交易日
  python scripts/new_official_template.py --date 2026-10-27
推估方式：自記帳 runs/state/ledger.json（前一結算日，若有官方檔則已以官方為準）＋ runs/<D>/ 的 D-Plan 委託以 D 日成交均價成交，
NAV 以 D 日收盤價計。草稿只是對照用：請逐項對照主辦方後台（淨值、現金、每檔股數），修正後另存為 runs/official/<D>.json
（刪除 "status": "draft"）。daily_run 只讀正式檔。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from esun_agent.agent.llm_budget import load_dotenv  # noqa: E402
from esun_agent.data.market import MARKET_DIR, load_daily  # noqa: E402
from esun_agent.official import (LEDGER_PATH, OFFICIAL_DIR, RUNS_DIR, load_ledger, official_path,  # noqa: E402
                                 project_to, team_id)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="結算日 D（預設 data/market 最新交易日）")
    ap.add_argument("--market-dir", type=Path, default=MARKET_DIR)
    ap.add_argument("--official-dir", type=Path, default=OFFICIAL_DIR)
    ap.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    ap.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    a = ap.parse_args(argv)
    load_dotenv()
    D = a.date or max(p.stem for p in a.market_dir.glob("????-??-??.parquet"))
    try:
        day = load_daily(D, a.market_dir)
    except FileNotFoundError:
        print(f"錯誤：沒有 {D} 的行情（data/market/{D}.parquet），請先跑 daily_run 或 backfill", file=sys.stderr)
        return 1
    avg = {t: float(v) for t, v in zip(day["ticker"], day["avg_price"]) if pd.notna(v)}
    close = {t: float(v) for t, v in zip(day["ticker"], day["close"]) if pd.notna(v)}
    warns: list[str] = []
    team = team_id(warns)
    est, w, applied = project_to(D, load_ledger(a.ledger), avg, close, team, a.runs_dir)
    out = {**est, "status": "draft",
           "_note": "草稿：請對照主辦方後台修正 nav、cash、holdings，刪除 status 與 _note 後另存為 " + official_path(D, a.official_dir).name}
    p = official_path(D, a.official_dir, draft=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已寫入草稿 {p}（{'已套用' if applied else '未套用'} {D} 的 D-Plan 委託）")
    print(f"  推估 NAV {est['nav']:,.0f}、現金 {est['cash']:,.0f}、持股 {len(est['holdings'])} 檔")
    for x in warns + w:
        print(f"  警告：{x}")
    print(f"對照後台修正後另存為 {official_path(D, a.official_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
