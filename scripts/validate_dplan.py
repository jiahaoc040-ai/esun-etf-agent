#!/usr/bin/env python3
"""提交前驗證 D-Plan：
  python scripts/validate_dplan.py <D-Plan.json> [--ctx context.json]
  python scripts/validate_dplan.py <D-Plan.json> --datapack <資料包.json>   # 帳務取自資料包，價格取 data/market 的 T-1 收盤
回傳碼 0=可提交，1=有 ERROR。"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from esun_agent.dplan_validate import validate  # noqa: E402


def datapack_ctx(path: str, trade_date: str, market_dir: Path | None = None) -> dict:
    from esun_agent.datapack import load_datapack, validate_ctx
    dp = load_datapack(Path(path), trade_date)
    close = {}
    if dp.as_of_date:
        try:
            from esun_agent.data.prices import load_panel
            panel = load_panel(market_dir)
            if dp.as_of_date in panel.dates:
                close = {t: float(v) for t, v in panel.close.loc[dp.as_of_date].dropna().items()}
        except Exception as e:  # noqa: BLE001
            print(f"（讀不到 {dp.as_of_date} 交易所收盤，改用資料包 close_price：{e}）")
    return validate_ctx(dp, close)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dplan")
    ap.add_argument("--ctx", help="帳務 context JSON（prev_nav / cash / holdings / prev_close）")
    ap.add_argument("--datapack", help="主辦方資料包 JSON（trade_date 須與 D-Plan 相同）")
    ap.add_argument("--market-dir", type=Path, help="交易所日行情資料夾（預設 data/market）")
    a = ap.parse_args(argv)
    doc = json.loads(Path(a.dplan).read_text(encoding="utf-8"))
    ctx = None
    if a.datapack:
        ctx = datapack_ctx(a.datapack, doc.get("trade_date"), a.market_dir)
    elif a.ctx:
        ctx = json.loads(Path(a.ctx).read_text(encoding="utf-8"))
    rep = validate(doc, ctx, filename=a.dplan)
    print(rep)
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
