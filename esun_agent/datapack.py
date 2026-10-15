"""主辦方資料包（DataPack，規格書 v4.3）：讀取、驗證、自動尋找、摘要。

DataPack 取代舊的 runs/official/<D>.json：每天 19:00 後到參賽者後台「文件繳交 → 下載資料包」，放進 ~/Downloads
（或 data/datapack/），daily_run 會自動找 doc_type=DataPack 且 trade_date=T 的最新一份，複製到 runs/<T>/raw/datapack.json。

帳務口徑（規格書 §2.4、§2.5）：
  - 分母一律 account.nav_tradable（不是 aum：aum 含應收配息／配股，不可用於比率）。
  - 現金 account.cash；持股 holdings[].shares（receivable_shares 應收配股不可賣、不計權重與檔數）。
  - missing 含 prev_settlement 或 account／holdings 為 null → 結算尚未產生：停止，**不可當成空手**。
  - as_of_date 為 null 且 missing 為空 → 第一個交易日：account 是真值（全現金、0 檔）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import LOT_SIZE, ROOT, passive_soft_limit
from .universe import load_universe

DATAPACK_DIR = ROOT / "data" / "datapack"
DOWNLOADS_DIR = Path.home() / "Downloads"
DOC_TYPE = "DataPack"
PASSIVE_WEIGHT_HINT = ("weight", "權重")          # incidents 的 check_code／message 含這些字 → 單一個股權重提示
NOT_SETTLED_MSG = "結算尚未產生，請稍後重新下載"
VOID_REASON_LABELS = {
    "insufficient_funds": "資金不足", "oversell": "賣出股數超過持股",
    "no_price": "委託標的缺當日成交均價", "compliance_fail": "合規檢查未通過（檔數／標的池／現金／當日加碼個股權重之一）",
    "dplan_reference_chain": "文件引用鏈不完整", "dplan_decision_coverage": "決策覆蓋不完整", "dplan_round_lot": "委託非整股",
}
OUTCOME_LABELS = {"none": "前一交易日未繳交", "unknown": "委託結果尚未判定", "voided": "整批作廢（未成交）",
                  "executed": "有成交"}


class DataPackError(ValueError):
    """資料包不能用（格式錯、不是本次 T、不是本隊）。"""


class NotSettledError(DataPackError):
    """前一交易日結算尚未產生（missing 含 prev_settlement 或 account 為 null）。"""


@dataclass
class DataPack:
    team_id: str
    trade_date: str
    as_of_date: str | None
    generated_at: str
    missing: list[str]
    nav: float                                       # account.nav_tradable
    cash: float                                      # account.cash
    holdings: dict[str, int]                         # shares > 0
    receivable: dict[str, int]                       # receivable_shares > 0（不可賣、不計權重）
    close: dict[str, float]                          # holdings[].close_price（as_of_date 收盤；null 不列）
    weights: dict[str, float]                        # holdings[].tradable_weight（null 不列）
    aum: float | None
    outcome: str
    void_reasons: list[str]
    excluded_codes: list[str]
    violations: int
    notices: int
    incidents: list[dict]
    raw: dict = field(repr=False, default_factory=dict)
    path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def first_day(self) -> bool:
        return self.as_of_date is None and not self.missing

    @property
    def alerts(self) -> list[dict]:
        """status 為 warn 或 fail 的 incidents。"""
        return [i for i in self.incidents if i.get("status") in ("warn", "fail")]

    def weight_incident_tickers(self, since: str | None = None) -> tuple[bool, set[str]]:
        """(是否有單一個股權重提示, 提示中出現的持股代號)。since：只看 date ≥ since 的列（incidents 是累計紀錄）。
        代號抓不到時回傳空集合（呼叫端改看全部超過內部上限的持股）。"""
        hit, codes = False, set()
        for i in self.alerts:
            if since and str(i.get("date") or "") < since:
                continue
            text = f"{i.get('check_code', '')} {i.get('message', '')}"
            if any(k in text.lower() for k in PASSIVE_WEIGHT_HINT):
                hit = True
                codes |= {c for c in re.findall(r"(?<!\d)(\d{4})(?!\d)", text) if c in self.holdings}
        return hit, codes


# --------------------------------------------------------------------------- 解析

def _num(x, what: str) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise DataPackError(f"{what} 必須是數字：{x!r}")
    return float(x)


def parse_datapack(d: dict, trade_date: str | None = None, team: str | None = None, path: Path | None = None) -> DataPack:
    """檢查並轉成 DataPack。trade_date／team 給定時必須相符。
    丟 NotSettledError：結算尚未產生；丟 DataPackError：其他不能用的情況。可疑但可用的情況放 warnings。"""
    if not isinstance(d, dict) or d.get("doc_type") != DOC_TYPE:
        raise DataPackError(f"doc_type 不是 {DOC_TYPE}：{(d or {}).get('doc_type')!r}" if isinstance(d, dict)
                            else "資料包不是 JSON 物件")
    for k in ("team_id", "trade_date", "missing", "prev_day_orders", "compliance"):
        if k not in d:
            raise DataPackError(f"資料包缺少 {k}")
    if trade_date is not None and d["trade_date"] != trade_date:
        raise DataPackError(f"資料包 trade_date {d['trade_date']} ≠ 本次交易日 T={trade_date}（請重新下載當日資料包）")
    if team is not None and d["team_id"] != team:
        raise DataPackError(f"資料包 team_id {d['team_id']} ≠ 本隊 TEAM_ID {team}")
    missing = list(d.get("missing") or [])
    acct, hold = d.get("account"), d.get("holdings")
    if "prev_settlement" in missing or acct is None or hold is None:
        raise NotSettledError(f"{NOT_SETTLED_MSG}（missing={missing}，account {'null' if acct is None else '有值'}，"
                              f"holdings {'null' if hold is None else '有值'}）——不可當成空手")
    warns: list[str] = []
    nav = _num(acct.get("nav_tradable"), "account.nav_tradable")
    cash = _num(acct.get("cash"), "account.cash")
    if nav <= 0:
        raise DataPackError(f"account.nav_tradable 必須 > 0：{nav}")
    if cash < 0:
        warns.append(f"資料包現金為負 {cash:,.0f}")
    uni = load_universe()
    holdings, receivable, close, weights = {}, {}, {}, {}
    for h in hold:
        t = str(h.get("code", "")).strip()
        n, r = h.get("shares", 0) or 0, h.get("receivable_shares", 0) or 0
        for v, what in ((n, "shares"), (r, "receivable_shares")):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) or v < 0:
                raise DataPackError(f"{t} 的 {what} 必須是非負整數：{v!r}")
        n, r = int(n), int(r)
        if n:
            holdings[t] = n
            if n % LOT_SIZE:
                warns.append(f"資料包庫存 {t} 股數 {n} 不是 1000 的倍數（零股？）")
        if r:
            receivable[t] = r
        if t not in uni:
            warns.append(f"資料包庫存 {t} 不在 150 檔名單")
        if h.get("close_price") is not None:
            close[t] = float(h["close_price"])
        elif n:
            warns.append(f"資料包 {t} 無收盤價（close_price=null，nav_tradable 以 0 計入）")
        if h.get("tradable_weight") is not None:
            weights[t] = float(h["tradable_weight"])
    if d.get("as_of_date") is None and not missing and holdings:
        raise DataPackError("as_of_date 為 null（第一個交易日）但 holdings 不是空的——資料包有誤，請重新下載")
    mv = sum(n * close.get(t, 0.0) for t, n in holdings.items())
    if abs(cash + mv - nav) > max(1.0, 1e-6 * nav):
        warns.append(f"資料包 nav_tradable {nav:,.0f} ≠ cash + Σ shares×close_price {cash + mv:,.0f}")
    if acct.get("holdings_count") is not None and int(acct["holdings_count"]) != len(holdings):
        warns.append(f"資料包 holdings_count {acct['holdings_count']} ≠ shares>0 的檔數 {len(holdings)}")
    if "prev_close" in missing:
        warns.append("資料包 missing 含 prev_close：主辦方收盤價尚未匯入，上傳後部分檢核會標為未驗（skipped）")
    other = [m for m in missing if m not in ("prev_close", "prev_settlement")]
    if other:
        warns.append(f"資料包 missing 含未知項目：{other}")
    pdo = d.get("prev_day_orders") or {}
    comp = d.get("compliance") or {}
    return DataPack(team_id=str(d["team_id"]), trade_date=str(d["trade_date"]), as_of_date=d.get("as_of_date"),
                    generated_at=str(d.get("generated_at") or ""), missing=missing, nav=nav, cash=cash,
                    holdings=holdings, receivable=receivable, close=close, weights=weights,
                    aum=acct.get("aum"), outcome=str(pdo.get("outcome") or "unknown"),
                    void_reasons=list(pdo.get("void_reasons") or []), excluded_codes=list(pdo.get("excluded_codes") or []),
                    violations=int(comp.get("violations") or 0), notices=int(comp.get("notices") or 0),
                    incidents=list(comp.get("incidents") or []), raw=d, path=path, warnings=warns)


def load_datapack(path: Path, trade_date: str | None = None, team: str | None = None) -> DataPack:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise DataPackError(f"讀不到資料包 {path}：{e}") from e
    return parse_datapack(d, trade_date, team, Path(path))


# --------------------------------------------------------------------------- 尋找

def _peek(p: Path) -> dict | None:
    try:
        if p.stat().st_size > 5_000_000:
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return d if isinstance(d, dict) and d.get("doc_type") == DOC_TYPE else None


def find_datapack(trade_date: str, dirs: list[Path] | None = None, errors: list[str] | None = None) -> Path | None:
    """在 dirs（預設 data/datapack/、~/Downloads/）找 doc_type=DataPack 且 trade_date=T 的最新檔
    （依 generated_at，再依檔案修改時間）。只看各資料夾第一層的 *.json。
    資料夾讀不到（例如 macOS 不允許 launchd 執行的 python 讀 ~/Downloads）時把原因加進 errors。"""
    found = []
    for base in dirs if dirs is not None else [DATAPACK_DIR, DOWNLOADS_DIR]:
        try:
            files = list(Path(base).glob("*.json")) if Path(base).is_dir() else []
        except OSError as e:
            if errors is not None:
                errors.append(f"無法讀取 {base}（{type(e).__name__}）——macOS 請到「系統設定 → 隱私權與安全性 → "
                              "完整磁碟取用權限」加入執行 daily_run 的 python／終端機，或把資料包放 data/datapack/")
            continue
        for p in files:
            d = _peek(p)
            if d and d.get("trade_date") == trade_date:
                found.append((str(d.get("generated_at") or ""), p.stat().st_mtime, p))
    return max(found, key=lambda x: (x[0], x[1]))[2] if found else None


# --------------------------------------------------------------------------- 對帳與摘要

def reconcile(led_holdings: dict[str, int], led_cash: float, dp: DataPack, cash_tol: float = 1.0) -> list[str]:
    """自記帳 vs 資料包逐檔對帳；回傳差異（以資料包為準）。"""
    diffs = []
    for t in sorted(set(led_holdings) | set(dp.holdings)):
        mine, theirs = led_holdings.get(t, 0), dp.holdings.get(t, 0)
        if mine != theirs:
            diffs.append(f"{t}: 自記 {mine:,} 股 ≠ 資料包 {theirs:,} 股（差 {theirs - mine:+,}）")
    if abs(led_cash - dp.cash) > cash_tol:
        diffs.append(f"現金: 自記 {led_cash:,.0f} ≠ 資料包 {dp.cash:,.0f}（差 {dp.cash - led_cash:+,.0f}）")
    return diffs


def overweight(dp: DataPack, close: dict[str, float]) -> dict[str, float]:
    """以 nav_tradable 與 T-1 收盤計，權重超過 9.5%（2330 超過 24%）的持股 → {代號: 權重}。"""
    out = {}
    for t, n in dp.holdings.items():
        px = close.get(t, dp.close.get(t))
        if px is None:
            continue
        w = n * px / dp.nav
        if w > passive_soft_limit(t):
            out[t] = w
    return out


def validate_ctx(dp: DataPack, close: dict[str, float] | None = None) -> dict:
    """資料包 → dplan_validate 的 context。close：交易所 T-1 收盤（缺的持股用資料包 close_price 補）。"""
    px = dict(dp.close)
    px.update(close or {})
    return {"prev_nav": dp.nav, "cash": dp.cash, "holdings": dict(dp.holdings), "prev_close": px}


def alert_lines(dp: DataPack) -> list[str]:
    """摘要最上方：compliance.incidents 的 warn／fail。"""
    out = []
    for i in dp.alerts:
        tag = "違規 FAIL" if i.get("status") == "fail" else "提示 WARN"
        out.append(f"⚠ [{tag}] {i.get('date', '')} {i.get('check_code', '')}：{i.get('message', '')}")
    return out


def summary_lines(dp: DataPack) -> list[str]:
    L = [f"資料包：{dp.path.name if dp.path else '(記憶體)'}｜trade_date {dp.trade_date}｜as_of {dp.as_of_date or 'null'}"
         f"｜產生於 {dp.generated_at}",
         f"  nav_tradable {dp.nav:,.0f}｜現金 {dp.cash:,.0f}（{dp.cash / dp.nav:.2%}）｜持股 {len(dp.holdings)} 檔"
         + (f"｜aum {dp.aum:,.0f}" if isinstance(dp.aum, (int, float)) else "")
         + (f"｜應收配股 {len(dp.receivable)} 檔（不可賣、不計權重）" if dp.receivable else "")]
    if dp.first_day:
        L.append("  第一個交易日：全現金建倉")
    L.append(f"  前一交易日委託：{dp.outcome}（{OUTCOME_LABELS.get(dp.outcome, '?')}）")
    if dp.outcome == "voided":
        for r in dp.void_reasons:
            L.append(f"    ✗ 作廢原因：{r}（{VOID_REASON_LABELS.get(r, '未知原因')}）")
    if dp.excluded_codes:
        L.append(f"    被排除未成交：{', '.join(dp.excluded_codes)}")
    L.append(f"  合規：違規 {dp.violations} 天、異常提示 {dp.notices} 列")
    if dp.missing:
        L.append(f"  missing：{dp.missing}")
    return L
