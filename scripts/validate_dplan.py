#!/usr/bin/env python3
"""提交前驗證 D-Plan：python scripts/validate_dplan.py <D-Plan.json> [--ctx context.json]
回傳碼 0=可提交，1=有 ERROR。"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from esun_agent.dplan_validate import validate  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("dplan")
ap.add_argument("--ctx", help="帳務 context JSON（prev_nav / cash / holdings / prev_close）")
a = ap.parse_args()
doc = json.loads(Path(a.dplan).read_text(encoding="utf-8"))
ctx = json.loads(Path(a.ctx).read_text(encoding="utf-8")) if a.ctx else None
rep = validate(doc, ctx, filename=a.dplan)
print(rep)
sys.exit(0 if rep.ok else 1)
