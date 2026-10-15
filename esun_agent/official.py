"""自記帳（runs/state/ledger.json）推估與隊伍代號。

主辦方結算帳務改由資料包（DataPack，esun_agent/datapack.py）提供，取代舊的手填 runs/official/<D>.json。
daily_run：自記帳推估到決策日 D 收盤（前一日帳務＋D 日 D-Plan 委託以 D 日均價成交）→ 與資料包逐檔對帳，
差異列出並以資料包為準；沒有資料包時用自記帳並大字警告「不可上傳」（C13 超賣風險：賣出股數必須 ≤ 結算庫存）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import INITIAL_CAPITAL, ROOT
from .ledger import Ledger

LEDGER_PATH = ROOT / "runs" / "state" / "ledger.json"
TEAM_TBD = "TEAM_TBD"


def team_id(warnings: list[str] | None = None) -> str:
    t = (os.environ.get("TEAM_ID") or "").strip()
    if not t:
        if warnings is not None:
            warnings.append("環境變數 TEAM_ID 未設定，暫用 TEAM_TBD——上傳前務必在 .env 設定正確的隊伍編號並重跑")
        return TEAM_TBD
    return t


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
    """前一日帳務＋當日 D-Plan 成交 → 當日收盤的推估帳務 {team_id, nav, cash, holdings}。"""
    led = Ledger(cash=prev.cash, holdings=dict(prev.holdings))
    warns = apply_dplan(led, dplan, avg_price) if dplan else []
    missing = [t for t in led.holdings if t not in close]
    if missing:
        warns.append(f"缺少收盤價：{missing}（NAV 推估不含這些部位）")
    nav = led.cash + sum(n * close[t] for t, n in led.holdings.items() if t in close)
    return {"team_id": team, "nav": round(nav, 2), "cash": round(led.cash, 2),
            "holdings": dict(sorted(led.holdings.items()))}, warns


RUNS_DIR = ROOT / "runs"


def find_dplan(trade_date: str, runs_dir: Path | None = None) -> dict | None:
    """runs/<trade_date>/D-Plan_*.json（最新的一份；--morning 重產時會覆寫同名檔）。"""
    files = sorted(((runs_dir or RUNS_DIR) / trade_date).glob("D-Plan_*.json"), key=lambda p: p.stat().st_mtime)
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def project_to(D: str, ledger: Ledger, avg_price: dict[str, float], close: dict[str, float], team: str,
               runs_dir: Path | None = None) -> tuple[dict, list[str], bool]:
    """自記帳（as_of 為 D 之前的結算日）＋ D 日 D-Plan 的委託 → D 日收盤推估。回傳 (推估帳務 dict, 警告, 是否套用了 D-Plan)。
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
