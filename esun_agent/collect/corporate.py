"""上市公司事件訊號（TWSE OpenAPI），只保留 universe_150 的 4 碼普通股：

  ex_dividend  /exchangeReport/TWT48U_ALL   除權除息預告：除權息日落在 T～T+3 個交易日 → 不新建倉
  insider      /opendata/t187ap12_L         內部人持股轉讓事前申報：近 5 個交易日有申報 → 信心降一級；
                                             轉讓股數合計占發行股數 > 0.5% → 不新建倉
  tv_promo     /Announcement/BFZFZU_T       投資理財節目異常推介個股：列入者不新建倉、已持有者信心降一級
  pledge       /opendata/t187ap09_L         董監質押：公司董監合計設質比例 > 50% → 信心降一級

上櫃對應端點（TPEx OpenAPI）尚未確認名稱，暫不抓取（見 docs/cloud_tasks.md 的待確認清單）。
這些端點的欄位名稱未在雲端驗證（fixture 為推測格式），parser 以關鍵字找欄位，首次在 Mac 執行請核對 raw 檔：
  代號：鍵名含 code／代號；日期：依各 kind 的偏好鍵名，否則第一個日期欄；股數／比例：鍵名含關鍵字。
收集只做解析；規則（T 日窗口、5 日、0.5%、50%）在 corporate_signals() 依實際交易日判斷。
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from ..universe import load_universe
from .http import Fetcher, decode
from .restrictions import STOCK_CODE_RE, _dates

BASE = "https://openapi.twse.com.tw/v1"
ENDPOINTS = {
    "ex_dividend": f"{BASE}/exchangeReport/TWT48U_ALL",
    "insider": f"{BASE}/opendata/t187ap12_L",
    "tv_promo": f"{BASE}/Announcement/BFZFZU_T",
    "pledge": f"{BASE}/opendata/t187ap09_L",
}
LABELS = {"ex_dividend": "除權除息預告", "insider": "內部人持股轉讓事前申報", "tv_promo": "投資理財節目異常推介",
          "pledge": "董監設質比例", "conference": "法人說明會"}
TPEX_TODO = ["除權除息預告（上櫃）", "內部人持股轉讓事前申報（上櫃）", "投資理財節目異常推介（上櫃）", "董監設質比例（上櫃）"]

EX_WINDOW, INSIDER_DAYS = 3, 5
INSIDER_PCT_MAX, PLEDGE_PCT_MAX = 0.5, 50.0
DATE_KEYS = {"ex_dividend": ("除權除息日", "exdividenddate", "exrightsdate", "exdate", "date", "日期"),
             "insider": ("申報日期", "declaredate", "reportdate", "date", "日期"),
             "tv_promo": ("date", "日期"), "pledge": ("date", "日期")}


def _code(r: dict) -> str | None:
    for k, v in r.items():
        if "code" in k.lower() or "代號" in k:
            v = str(v).strip()
            return v if STOCK_CODE_RE.fullmatch(v) else None
    return None


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _date(r: dict, kind: str) -> date | None:
    norm = {re.sub(r"[^0-9a-z一-鿿]", "", k.lower()): v for k, v in r.items()}
    for pref in DATE_KEYS[kind]:
        for k, v in norm.items():
            if pref in k:
                ds = _dates(v)
                if ds:
                    return ds[0]
    return None


def _first(r: dict, pred) -> float | None:
    for k, v in r.items():
        if pred(k):
            x = _num(v)
            if x is not None:
                return x
    return None


def parse_corporate(records, kind: str, universe: dict | None = None) -> dict[str, dict]:
    """OpenAPI 陣列 → {ticker: 資訊}。insider 同一檔多筆申報：股數加總、日期取最新；pledge：設質股數÷持股數合計
    （沒有這兩欄時取比例欄的最大值）。"""
    uni = universe if universe is not None else load_universe()
    if not isinstance(records, list):
        raise ValueError(f"{kind} 回應不是陣列：{json.dumps(records, ensure_ascii=False)[:80]}")
    out: dict[str, dict] = {}
    for r in records:
        if not isinstance(r, dict):
            continue
        t = _code(r)
        if t is None or t not in uni:
            continue
        d = _date(r, kind)
        if kind == "ex_dividend":
            if d:
                cur = out.get(t)
                if cur is None or d.isoformat() < cur["date"]:
                    out[t] = {"date": d.isoformat()}
        elif kind == "insider":
            sh = _first(r, lambda k: "轉讓" in k and "股數" in k) or _first(r, lambda k: "shares" in k.lower()) or 0.0
            cur = out.setdefault(t, {"date": None, "shares": 0.0, "n": 0})
            cur["shares"] += sh
            cur["n"] += 1
            if d and (cur["date"] is None or d.isoformat() > cur["date"]):
                cur["date"] = d.isoformat()
        elif kind == "tv_promo":
            out[t] = {"date": d.isoformat() if d else None}
        elif kind == "pledge":
            held = _first(r, lambda k: ("持股" in k and "設質" not in k and "質" not in k) or "currentshares" in k.lower())
            pledged = _first(r, lambda k: ("設質股數" in k or "質權設定股數" in k) or "pledgedshares" in k.lower())
            ratio = _first(r, lambda k: ("設質" in k or "質押" in k) and ("比" in k or "%" in k) or "pledgeratio" in k.lower())
            cur = out.setdefault(t, {"held": 0.0, "pledged": 0.0, "max_ratio": 0.0})
            if held is not None and pledged is not None:
                cur["held"] += held
                cur["pledged"] += pledged
            if ratio is not None:
                cur["max_ratio"] = max(cur["max_ratio"], ratio)
    for t, x in list(out.items()):
        if kind == "pledge":
            x["ratio_pct"] = round(x["pledged"] / x["held"] * 100, 4) if x["held"] > 0 else x["max_ratio"]
        if kind == "insider" and x["date"] is None:
            out.pop(t)
    return out


def collect_corporate(fetcher: Fetcher, universe: dict | None = None) -> tuple[dict, list[str]]:
    """回傳 ({kind: {ticker: info}, "sources": [{kind, url}]}, warnings)。任一端點失敗只記警告。"""
    res: dict = {k: {} for k in ENDPOINTS} | {"sources": []}
    warns: list[str] = []
    for kind, url in ENDPOINTS.items():
        try:
            raw = fetcher.get(url, name=f"corp_{kind}.json", retries=2)
            res[kind] = parse_corporate(json.loads(decode(raw)), kind, universe)
            res["sources"].append({"kind": kind, "url": url})
        except Exception as e:  # noqa: BLE001
            warns.append(f"上市{LABELS[kind]}抓取失敗（{type(e).__name__}: {str(e).splitlines()[0][:80]}），該類缺值")
    return res, warns


def _add_weekdays(d: date, n: int) -> date:
    step = 1 if n >= 0 else -1
    while n:
        d += timedelta(days=step)
        if d.weekday() < 5:
            n -= step
    return d


def corporate_signals(corp: dict | None, conferences: list[dict], D: str, T: str, shares=None
                      ) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list[tuple[str, str]]]]:
    """依實際交易日判斷。回傳 (no_new, downgrade)：{ticker: [(kind, 說明)]}。
    conferences = [{"ticker", "date", ...}]（event_calendar.stock_events 的法說會）。"""
    corp = corp or {}
    Td, Dd = date.fromisoformat(T), date.fromisoformat(D)
    no_new: dict[str, list[tuple[str, str]]] = {}
    down: dict[str, list[tuple[str, str]]] = {}
    end = _add_weekdays(Td, EX_WINDOW)
    for t, x in (corp.get("ex_dividend") or {}).items():
        if T <= x["date"] <= end.isoformat():
            no_new.setdefault(t, []).append(("ex_dividend", f"{x['date']} 除權息（T～T+{EX_WINDOW} 個交易日內）"))
    since = _add_weekdays(Dd, -(INSIDER_DAYS - 1)).isoformat()
    for t, x in (corp.get("insider") or {}).items():
        if since <= x["date"] <= D:
            down.setdefault(t, []).append(("insider", f"{x['date']} 有內部人持股轉讓事前申報"))
            sh = None if shares is None else shares.get(t)
            if sh and x["shares"] / sh * 100 > INSIDER_PCT_MAX:
                no_new.setdefault(t, []).append(
                    ("insider", f"申報轉讓 {x['shares']:,.0f} 股，占發行股數 {x['shares'] / sh * 100:.2f}% > {INSIDER_PCT_MAX}%"))
    for t, x in (corp.get("tv_promo") or {}).items():
        no_new.setdefault(t, []).append(("tv_promo", "列入投資理財節目異常推介個股"))
        down.setdefault(t, []).append(("tv_promo", "列入投資理財節目異常推介個股"))
    for t, x in (corp.get("pledge") or {}).items():
        if x.get("ratio_pct", 0.0) > PLEDGE_PCT_MAX:
            down.setdefault(t, []).append(("pledge", f"董監設質比例 {x['ratio_pct']:.1f}% > {PLEDGE_PCT_MAX:.0f}%"))
    nxt = _add_weekdays(Td, 1).isoformat()
    for c in conferences or []:
        if T < c["date"] <= nxt:
            no_new.setdefault(c["ticker"], []).append(("conference", f"{c['date']} 召開法人說明會（法說前 1 個交易日）"))
    return no_new, down
