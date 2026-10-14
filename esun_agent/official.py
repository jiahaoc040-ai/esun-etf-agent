"""主辦方結算帳務（官方庫存、現金、淨值）輸入與對帳。

SS 每天晚上（主辦方 19:00 更新淨值後）把後台數字填進 runs/official/<結算日 D>.json：
  {"team_id": "TEAM_042", "nav": 1003456789.0, "cash": 31234567.0, "holdings": {"2330": 120000, ...}}
scripts/new_official_template.py 會依「前一日官方帳務＋當日 D-Plan 委託以 D 日均價成交」推估出草稿
runs/official/<D>.draft.json，SS 對照後台修正後另存為 <D>.json（草稿不會被 daily_run 當成官方資料）。

daily_run：自記帳 runs/state/ledger.json（推估）與官方檔對帳，差異逐條列出並以官方為準；官方檔不存在時
用自記帳並大字警告（C13 超賣風險：賣出股數必須 ≤ 官方庫存）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import INITIAL_CAPITAL, LOT_SIZE, ROOT
from .ledger import Ledger
from .universe import load_universe

OFFICIAL_DIR = ROOT / "runs" / "official"
LEDGER_PATH = ROOT / "runs" / "state" / "ledger.json"
TEAM_TBD = "TEAM_TBD"


def team_id(warnings: list[str] | None = None) -> str:
    t = (os.environ.get("TEAM_ID") or "").strip()
    if not t:
        if warnings is not None:
            warnings.append("環境變數 TEAM_ID 未設定，暫用 TEAM_TBD——上傳前務必在 .env 設定正確的隊伍編號並重跑")
        return TEAM_TBD
    return t


@dataclass
class Official:
    date: str
    team_id: str
    nav: float
    cash: float
    holdings: dict[str, int]
    warnings: list[str] = field(default_factory=list)


def official_path(d: str, base: Path | None = None, draft: bool = False) -> Path:
    return (base or OFFICIAL_DIR) / f"{d}{'.draft' if draft else ''}.json"


def parse_official(d: dict, date_str: str) -> Official:
    """檢查格式（丟 ValueError 表示不能用）；可疑但可用的情況放 warnings。"""
    errs, warns = [], []
    for k in ("team_id", "nav", "cash", "holdings"):
        if k not in d:
            errs.append(f"缺少 {k}")
    if errs:
        raise ValueError("；".join(errs))
    if d.get("status") == "draft" or d.get("_draft"):
        raise ValueError("這是草稿（status=draft），請對照後台修正並移除 status 後再存成正式檔")
    try:
        nav, cash = float(d["nav"]), float(d["cash"])
    except (TypeError, ValueError):
        raise ValueError("nav、cash 必須是數字") from None
    if not isinstance(d["holdings"], dict):
        raise ValueError("holdings 必須是 {ticker: 股數}")
    uni = load_universe()
    hold = {}
    for t, n in d["holdings"].items():
        t = str(t).strip()
        if isinstance(n, bool) or not isinstance(n, (int, float)) or n != int(n) or n < 0:
            raise ValueError(f"{t} 的股數必須是非負整數：{n!r}")
        if t not in uni:
            warns.append(f"官方庫存 {t} 不在 150 檔名單")
        if int(n) % LOT_SIZE:
            warns.append(f"官方庫存 {t} 股數 {int(n)} 不是 1000 的倍數（零股？）")
        if int(n):
            hold[t] = int(n)
    if nav <= 0:
        raise ValueError("nav 必須 > 0")
    if cash < 0:
        warns.append(f"官方現金為負 {cash:,.0f}")
    return Official(date_str, str(d["team_id"]), nav, cash, hold, warns)


def load_official(date_str: str, base: Path | None = None) -> Official | None:
    p = official_path(date_str, base)
    if not p.exists():
        return None
    return parse_official(json.loads(p.read_text(encoding="utf-8")), date_str)


# --------------------------------------------------------------------------- 推估（自記帳）

def load_ledger(path: Path | None = None) -> Ledger:
    p = path or LEDGER_PATH
    return Ledger.load(p) if p.exists() else Ledger(cash=float(INITIAL_CAPITAL))


def apply_dplan(led: Ledger, dplan: dict, avg_price: dict[str, float]) -> list[str]:
    """把某交易日 D-Plan 的委託以該日均價成交（無均價者視為未成交）。回傳警告。"""
    warns, fills, px = [], [], {}
    for o in dplan.get("orders", []):
        p = avg_price.get(o["ticker"])
        if p is None or p != p:
            warns.append(f"{o['ticker']} 當日無成交均價，{o['side']} {o['shares']} 視為未成交")
            continue
        n = o["shares"]
        if o["side"] == "SELL" and n > led.holdings.get(o["ticker"], 0):
            warns.append(f"{o['ticker']} 賣出 {n} 股 > 自記庫存 {led.holdings.get(o['ticker'], 0)}，以庫存為限")
            n = led.holdings.get(o["ticker"], 0)
        if n:
            fills.append({"ticker": o["ticker"], "side": o["side"], "shares": n})
            px[o["ticker"]] = float(p)
    led.apply_fills(fills, px)
    return warns


def estimate(prev: Ledger, dplan: dict | None, avg_price: dict[str, float], close: dict[str, float],
             date_str: str, team: str) -> tuple[dict, list[str]]:
    """前一日帳務＋當日 D-Plan 成交 → 當日收盤的推估帳務（官方格式）。"""
    led = Ledger(cash=prev.cash, holdings=dict(prev.holdings))
    warns = apply_dplan(led, dplan, avg_price) if dplan else []
    missing = [t for t in led.holdings if t not in close]
    if missing:
        warns.append(f"缺少收盤價：{missing}（NAV 推估不含這些部位）")
    nav = led.cash + sum(n * close[t] for t, n in led.holdings.items() if t in close)
    return {"team_id": team, "nav": round(nav, 2), "cash": round(led.cash, 2),
            "holdings": dict(sorted(led.holdings.items()))}, warns


def reconcile(ledger: Ledger, off: Official, est_nav: float | None = None, nav_tol: float = 0.001) -> list[str]:
    diffs = ledger.reconcile(off.holdings, off.cash)
    if est_nav is not None and abs(est_nav - off.nav) > nav_tol * off.nav:
        diffs.append(f"NAV：自記推估 {est_nav:,.0f} ≠ 官方 {off.nav:,.0f}（差 {est_nav / off.nav - 1:+.2%}）")
    return diffs


RUNS_DIR = ROOT / "runs"


def find_dplan(trade_date: str, runs_dir: Path | None = None) -> dict | None:
    """runs/<trade_date>/D-Plan_*.json（最新的一份；--morning 重產時會覆寫同名檔）。"""
    files = sorted(((runs_dir or RUNS_DIR) / trade_date).glob("D-Plan_*.json"), key=lambda p: p.stat().st_mtime)
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def project_to(D: str, ledger: Ledger, avg_price: dict[str, float], close: dict[str, float], team: str,
               runs_dir: Path | None = None) -> tuple[dict, list[str], bool]:
    """自記帳（as_of 為 D 之前的結算日）＋ D 日 D-Plan 的委託 → D 日收盤推估。回傳 (官方格式 dict, 警告, 是否套用了 D-Plan)。
    ledger.as_of 已經是 D（或更晚）時不再套用委託。"""
    warns = []
    if ledger.as_of and ledger.as_of >= D:
        if ledger.as_of > D:
            warns.append(f"自記帳已是 {ledger.as_of}（晚於 {D}），不再套用 {D} 的委託")
        dplan = None
    else:
        dplan = find_dplan(D, runs_dir)
        if dplan is None and ledger.holdings:
            warns.append(f"找不到交易日 {D} 的 D-Plan（runs/{D}/），假設當日沒有交易")
    est, w = estimate(ledger, dplan, avg_price, close, D, team)
    return est, warns + w, dplan is not None
