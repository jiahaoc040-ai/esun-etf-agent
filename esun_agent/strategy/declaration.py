"""config/strategy_declaration.json 的讀取與驗證（T4 的 prompt 與 T6 的說明文件都讀這份）。

JSON 內的 portfolio_rules 必須與 FinalParams 預設值一致：程式參數與對外宣告只有一個真相來源，
改參數卻忘了改宣告（或反過來）會在測試中失敗，避免「宣告的策略」與「實際跑的策略」不同。
"""
from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

from ..config import ROOT
from .final import FinalParams

DECLARATION_PATH = ROOT / "config" / "strategy_declaration.json"
REQUIRED_KEYS = ["version", "etf", "theme", "philosophy", "benchmark", "portfolio_rules", "rebalance_policy", "tilt"]
TILT_BASIS_IDS = {"financial_report", "investor_conference", "monthly_revenue", "trust_flow", "material_news"}
SOURCE_AUTHORITIES = {"twse", "tpex", "taifex", "mops", "fininst", "media", "vendor"}


def load_declaration(path: Path | None = None) -> dict:
    d = json.loads((path or DECLARATION_PATH).read_text(encoding="utf-8"))
    errs = validate_declaration(d)
    if errs:
        raise ValueError("strategy_declaration.json 不合法：" + "；".join(errs))
    return d


def validate_declaration(d: dict) -> list[str]:
    errs = [f"缺少欄位 {k}" for k in REQUIRED_KEYS if k not in d]
    if errs:
        return errs
    defaults = FinalParams()
    rules = d["portfolio_rules"]
    for f in fields(defaults):
        if f.name == "ap_max_iter":
            continue
        if f.name not in rules:
            errs.append(f"portfolio_rules 缺 {f.name}")
        elif rules[f.name] != getattr(defaults, f.name):
            errs.append(f"portfolio_rules.{f.name}={rules[f.name]} 與程式預設 {getattr(defaults, f.name)} 不一致")
    bases = d["tilt"].get("allowed_bases", [])
    ids = {b.get("id") for b in bases}
    if ids != TILT_BASIS_IDS:
        errs.append(f"tilt.allowed_bases 的 id 應為 {sorted(TILT_BASIS_IDS)}，實際 {sorted(i for i in ids if i)}")
    for b in bases:
        if not b.get("authority") or not set(b["authority"]) <= SOURCE_AUTHORITIES:
            errs.append(f"{b.get('id')} 的 authority 不合法: {b.get('authority')}")
    if d["tilt"]["limits"].get("per_name_abs_delta") != defaults.max_delta:
        errs.append("tilt.limits.per_name_abs_delta 與 FinalParams.max_delta 不一致")
    return errs
