#!/usr/bin/env python
"""產生 ETF 投資策略說明文件 → output/strategy_doc.docx（保留）＋ output/strategy_doc.pdf（上傳後台用，主辦方只收 PDF）。

  python scripts/make_strategy_doc.py                     # 官方表單 + 補充說明，docx + pdf
  python scripts/make_strategy_doc.py --form-only         # 只有官方三欄表單
  python scripts/make_strategy_doc.py --pdf-engine reportlab   # 不用 LibreOffice
名稱、主題、理念讀 config/strategy_declaration.json（須符合官方格式限制）；回測數字讀 docs/conviction_backtest.json
（由 scripts/run_conviction_backtest.py 產生）。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.strategy_doc import OUTPUT_PATH, DocError, build_strategy_doc, docx_to_pdf  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUTPUT_PATH, help="docx 路徑；PDF 存在同名 .pdf")
    ap.add_argument("--form-only", action="store_true")
    ap.add_argument("--pdf-engine", choices=("auto", "soffice", "reportlab"), default="auto")
    ap.add_argument("--no-pdf", action="store_true", help="只產生 docx")
    a = ap.parse_args(argv)
    try:
        p = build_strategy_doc(a.out, form_only=a.form_only)
        print(f"已產生 {p}")
        if not a.no_pdf:
            pdf = docx_to_pdf(p, p.with_suffix(".pdf"), a.pdf_engine)
            print(f"已產生 {pdf}（上傳到後台「文件繳交 → ETF 投資策略說明文件」）")
    except DocError as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
