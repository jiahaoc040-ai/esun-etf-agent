#!/usr/bin/env python
"""產生 ETF 投資策略說明文件 → output/strategy_doc.docx。

  python scripts/make_strategy_doc.py              # 官方表單 + 補充說明
  python scripts/make_strategy_doc.py --form-only  # 只有官方三欄表單
名稱、主題、理念讀 config/strategy_declaration.json（須符合官方格式限制）；回測數字讀 docs/final_strategy_backtest.json
（由 scripts/run_final_backtest.py 產生）。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.strategy_doc import OUTPUT_PATH, DocError, build_strategy_doc  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUTPUT_PATH)
    ap.add_argument("--form-only", action="store_true")
    a = ap.parse_args(argv)
    try:
        p = build_strategy_doc(a.out, form_only=a.form_only)
    except DocError as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 1
    print(f"已產生 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
