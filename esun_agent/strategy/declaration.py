"""config/strategy_declaration.json 的讀取與驗證（T4 的 prompt 與 T6 的說明文件都讀這份）。

v2（集中主題＋事件驅動）：portfolio_rules ↔ ConvictionParams、candidate_rules ↔ CandidateParams、
fallback.portfolio_rules ↔ FinalParams（降級保底：合規基準 A）必須與程式預設值一致：程式參數與對外宣告只有一個真相來源，
改參數卻忘了改宣告（或反過來）會在測試中失敗，避免「宣告的策略」與「實際跑的策略」不同。
"""
from __future__ import annotations

import json
import re
from dataclasses import fields
from pathlib import Path

from ..config import ROOT
from .candidates import CandidateParams
from .conviction import ConvictionParams
from .final import FinalParams

DECLARATION_PATH = ROOT / "config" / "strategy_declaration.json"
REQUIRED_KEYS = ["version", "etf", "theme", "philosophy", "factors", "benchmark", "portfolio_rules", "candidate_rules",
                 "rebalance_policy", "selection", "discipline", "fallback"]
TILT_BASIS_IDS = {"financial_report", "investor_conference", "monthly_revenue", "trust_flow", "material_news"}
SOURCE_AUTHORITIES = {"twse", "tpex", "taifex", "mops", "fininst", "media", "vendor"}
REQUIRED_FACTORS = {"trust_flow", "us_events", "macro", "retail_attention"}
FACTOR_ROLES = {"primary", "supporting", "risk_overlay", "auxiliary"}
MACRO_SERIES = ["DGS2", "DFF", "VIXCLS", "T10Y2Y"]
MACRO_THRESHOLDS = {"vix_max": 25.0, "dgs2_daily_change_bp_max": 15.0}   # 與 observations.macro_risk_flags 預設一致


def load_declaration(path: Path | None = None) -> dict:
    d = json.loads((path or DECLARATION_PATH).read_text(encoding="utf-8"))
    errs = validate_declaration(d)
    if errs:
        raise ValueError("strategy_declaration.json 不合法：" + "；".join(errs))
    return d


NAME_RE = re.compile(r"^主動[一-鿿A-Za-z ]*$")
NAME_MAX, THEME_MAX, PHIL_MIN, PHIL_MAX = 20, 50, 100, 300


def validate_submission(d: dict) -> list[str]:
    """官方「ETF 投資策略說明文件」欄位限制：名稱 ≤20 字、限中英文、開頭須為「主動」；主題 ≤50 字；理念 100～300 字。"""
    errs = []
    name = str(d.get("etf", {}).get("name", ""))
    if not name.startswith("主動"):
        errs.append("etf.name 開頭須為「主動」二字（官方格式）")
    if len(name) > NAME_MAX:
        errs.append(f"etf.name 超過 {NAME_MAX} 字（{len(name)}）")
    if not NAME_RE.match(name):
        errs.append("etf.name 限使用中文或英文（官方格式）")
    theme = str(d.get("theme", {}).get("title", ""))
    if not 1 <= len(theme) <= THEME_MAX:
        errs.append(f"theme.title 需在 1–{THEME_MAX} 字（{len(theme)}）")
    phil = str(d.get("philosophy_statement", ""))
    if not PHIL_MIN <= len(phil) <= PHIL_MAX:
        errs.append(f"philosophy_statement 需在 {PHIL_MIN}～{PHIL_MAX} 字（{len(phil)}）")
    return errs


def allowed_bases(decl: dict) -> list[dict]:
    """五種可引用的依據（id、label、authority…）。"""
    return decl["selection"]["allowed_bases"]


def validate_factors(factors) -> list[str]:
    """宣告因子：必須含 trust_flow（首要）、us_events、macro、retail_attention；散戶關注度只能升降一級、不單獨觸發買賣；
    macro 數列與風險門檻必須與程式一致（collect/fred.MACRO_SERIES、observations.macro_risk_flags）。"""
    if not isinstance(factors, list):
        return ["factors 應為陣列"]
    by_id = {f.get("id"): f for f in factors}
    errs = [f"factors 缺 {i}" for i in sorted(REQUIRED_FACTORS - set(by_id))]
    for f in factors:
        if f.get("role") not in FACTOR_ROLES:
            errs.append(f"factors.{f.get('id')}.role 不合法：{f.get('role')!r}")
        if not f.get("sources") or not set(f["sources"]) <= SOURCE_AUTHORITIES | {"other"}:
            errs.append(f"factors.{f.get('id')}.sources 不合法：{f.get('sources')}")
    if "trust_flow" in by_id and by_id["trust_flow"].get("role") != "primary":
        errs.append("factors.trust_flow 必須是 primary（首要訊號）")
    ra = by_id.get("retail_attention")
    if ra and (ra.get("role") != "auxiliary" or ra.get("max_conviction_shift") != 1 or ra.get("can_trigger_trade") is not False):
        errs.append("factors.retail_attention 必須是 auxiliary、max_conviction_shift=1、can_trigger_trade=false")
    m = by_id.get("macro")
    if m:
        from ..collect.fred import MACRO_SERIES as CODE_SERIES
        if m.get("series") != list(CODE_SERIES) or m.get("series") != MACRO_SERIES:
            errs.append(f"factors.macro.series 應為 {MACRO_SERIES}（與 collect/fred.py 一致）：{m.get('series')}")
        if m.get("risk_thresholds") != MACRO_THRESHOLDS:
            errs.append(f"factors.macro.risk_thresholds 應為 {MACRO_THRESHOLDS}：{m.get('risk_thresholds')}")
    return errs


def _match(rules: dict, obj, where: str, skip=()) -> list[str]:
    errs = []
    for f in fields(obj):
        if f.name in skip:
            continue
        want = getattr(obj, f.name)
        want = list(want) if isinstance(want, tuple) else want
        if f.name not in rules:
            errs.append(f"{where} 缺 {f.name}")
        elif rules[f.name] != want:
            errs.append(f"{where}.{f.name}={rules[f.name]} 與程式預設 {want} 不一致")
    return errs


def validate_declaration(d: dict) -> list[str]:
    errs = [f"缺少欄位 {k}" for k in REQUIRED_KEYS if k not in d]
    if errs:
        return errs
    errs += validate_submission(d)
    errs += validate_factors(d["factors"])
    errs += _match(d["portfolio_rules"], ConvictionParams(), "portfolio_rules")
    errs += _match(d["candidate_rules"], CandidateParams(), "candidate_rules")
    errs += _match(d["fallback"].get("portfolio_rules", {}), FinalParams(), "fallback.portfolio_rules", skip=("ap_max_iter",))
    if d.get("status") not in ("draft", "final"):
        errs.append(f"status 應為 draft 或 final: {d.get('status')!r}")
    elif d["status"] == "final":
        if not str(d["etf"].get("name", "")).strip() or d["etf"].get("name_status") != "final":
            errs.append("status=final 時 etf.name 不可為空且 etf.name_status 必須是 final")
        if any(b.get("authority_status") != "confirmed" for b in d["selection"].get("allowed_bases", [])):
            errs.append("status=final 時每個依據的 authority_status 都必須是 confirmed")
    bases = d["selection"].get("allowed_bases", [])
    ids = {b.get("id") for b in bases}
    if ids != TILT_BASIS_IDS:
        errs.append(f"selection.allowed_bases 的 id 應為 {sorted(TILT_BASIS_IDS)}，實際 {sorted(i for i in ids if i)}")
    for b in bases:
        if not b.get("authority") or not set(b["authority"]) <= SOURCE_AUTHORITIES:
            errs.append(f"{b.get('id')} 的 authority 不合法: {b.get('authority')}")
    if set(d["selection"].get("conviction_levels", {})) != {"high", "medium"}:
        errs.append("selection.conviction_levels 需為 high 與 medium")
    if d["fallback"].get("tilt_limits", {}).get("per_name_abs_delta") != FinalParams().max_delta:
        errs.append("fallback.tilt_limits.per_name_abs_delta 與 FinalParams.max_delta 不一致")
    return errs
