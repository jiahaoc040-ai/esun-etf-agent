"""上市公司事件訊號（TWSE OpenAPI），只保留 universe_150 的 4 碼普通股：

  ex_dividend  /exchangeReport/TWT48U_ALL   除權除息預告：除權息日落在 T～T+3 個交易日 → 不新建倉
  insider      /opendata/t187ap12_L         內部人持股轉讓事前申報：近 5 個交易日有申報 → 信心降一級；
                                             轉讓股數合計占發行股數 > 0.5% → 不新建倉。端點只回當日出表的資料
                                             （實測 10/16 只有 3 筆），所以每天存 data/corporate/insider/YYYY-MM-DD.json，
                                             「近 5 個交易日」由累積的檔案判斷（不足 5 天用現有天數並記 info）。
                                             實測欄位為中文：出表日期、公司代號、預定轉讓總股數-自有持股…
  tv_promo     /Announcement/BFZFZU_T       投資理財節目異常推介個股：列入者不新建倉、已持有者信心降一級
  pledge       /opendata/t187ap09_L         董監質押（月資料，出表日期如 1150819）：依設質比例區間分組，各組的公司清單放在
                                             一個文字欄位（每行「3040      遠見  99.36\r\n」＝代號、簡稱、比例%）；
                                             比例 > 50% → 信心降一級

上櫃對應端點（TPEx OpenAPI）尚未確認名稱，暫不抓取（見 docs/cloud_tasks.md 的待確認清單）。
這些端點的欄位名稱未在雲端驗證（fixture 為推測格式），parser 以關鍵字找欄位，首次在 Mac 執行請核對 raw 檔：
  代號：鍵名含 code／代號；日期：依各 kind 的偏好鍵名，否則第一個日期欄；股數／比例：鍵名含關鍵字。
收集只做解析；規則（T 日窗口、5 日、0.5%、50%）在 corporate_signals() 依實際交易日判斷。
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from ..config import ROOT
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

INSIDER_DIR = ROOT / "data" / "corporate" / "insider"
PLEDGE_LINE_RE = re.compile(r"(?m)^[ \t]*(\d{4})[ \t]+(.+?)[ \t]+(\d+(?:\.\d+)?)[ \t]*$")
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


def insider_shares(r: dict) -> float:
    """預定轉讓股數：有「合計／總計」欄用它，否則把所有「預定轉讓總股數-…」（自有持股、信託…）加總。"""
    tot = _first(r, lambda k: "轉讓" in k and "股數" in k and ("合計" in k or "總計" in k))
    if tot is not None:
        return tot
    parts = [x for k, v in r.items() if "轉讓" in k and "股數" in k and "每日" not in k and (x := _num(v)) is not None]
    return float(sum(parts)) if parts else (_first(r, lambda k: "shares" in k.lower()) or 0.0)


def parse_pledge(records, universe: dict) -> dict[str, dict]:
    """t187ap09_L：每列一個設質比例區間，公司清單在文字欄位（「3040      遠見  99.36\r\n」每行一家）。
    → {ticker: {"ratio_pct", "as_of"}}（只留 universe；同一檔出現多次取最大比例）。"""
    out: dict[str, dict] = {}
    for r in records:
        if not isinstance(r, dict):
            continue
        as_of = None
        for k, v in r.items():
            if "日期" in k or "date" in k.lower():
                ds = _dates(v)
                as_of = ds[0].isoformat() if ds else as_of
        for v in r.values():
            for code, _name, pct in PLEDGE_LINE_RE.findall(str(v).replace("\r", "")):
                if code in universe:
                    x = float(pct)
                    if code not in out or x > out[code]["ratio_pct"]:
                        out[code] = {"ratio_pct": x, "as_of": as_of}
    return out


def save_insider_day(records: list, day: str, base: Path | None = None) -> Path:
    """當日出表的內部人申報原始回應存成 data/corporate/insider/<day>.json（day 取出表日期，沒有時用決策日）。"""
    p = (base or INSIDER_DIR) / f"{day}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def insider_out_date(records: list, default: str) -> str:
    for r in records or []:
        if isinstance(r, dict):
            for k, v in r.items():
                if "出表日期" in k:
                    ds = _dates(v)
                    if ds:
                        return ds[0].isoformat()
    return default


def load_insider_window(D: str, base: Path | None = None, universe: dict | None = None, days: int = INSIDER_DAYS
                        ) -> tuple[dict[str, dict], list[str]]:
    """讀 [D 往前 days 個交易日（平日近似）, D] 內累積的每日檔 → 合併成 {ticker: {date, shares, n}}（日期＝檔案日期）。
    回傳 (insider, 使用到的檔案日期)。"""
    base = base or INSIDER_DIR
    lo = _add_weekdays(date.fromisoformat(D), -(days - 1)).isoformat()
    used, out = [], {}
    for p in sorted(base.glob("????-??-??.json")) if base.exists() else []:
        if not lo <= p.stem <= D:
            continue
        used.append(p.stem)
        for t, x in parse_corporate(json.loads(p.read_text(encoding="utf-8")), "insider", universe).items():
            cur = out.setdefault(t, {"date": p.stem, "shares": 0.0, "n": 0})
            cur["shares"] += x["shares"]
            cur["n"] += x["n"]
            cur["date"] = max(cur["date"], p.stem)
    return out, used


def parse_corporate(records, kind: str, universe: dict | None = None) -> dict[str, dict]:
    """OpenAPI 陣列 → {ticker: 資訊}。insider 同一檔多筆申報：股數加總、日期取最新；pledge：設質股數÷持股數合計
    （沒有這兩欄時取比例欄的最大值）。"""
    uni = universe if universe is not None else load_universe()
    if not isinstance(records, list):
        raise ValueError(f"{kind} 回應不是陣列：{json.dumps(records, ensure_ascii=False)[:80]}")
    if kind == "pledge":
        return parse_pledge(records, uni)
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
            sh = insider_shares(r)
            cur = out.setdefault(t, {"date": None, "shares": 0.0, "n": 0})
            cur["shares"] += sh
            cur["n"] += 1
            if d and (cur["date"] is None or d.isoformat() > cur["date"]):
                cur["date"] = d.isoformat()
        elif kind == "tv_promo":
            out[t] = {"date": d.isoformat() if d else None}
    for t, x in list(out.items()):
        if kind == "insider" and x["date"] is None:
            out.pop(t)
    return out


def collect_corporate(fetcher: Fetcher, D: date | None = None, universe: dict | None = None,
                      insider_dir: Path | None = None) -> tuple[dict, list[str], list[str]]:
    """回傳 ({kind: {ticker: info}, "sources": [{kind, url}], "insider_days": [...]}, warnings, info)。
    任一端點失敗只記警告。內部人申報：當日回應存檔後，以累積檔案（近 5 個交易日）判斷。"""
    D = D or date.today()
    res: dict = {k: {} for k in ENDPOINTS} | {"sources": []}
    warns: list[str] = []
    info: list[str] = []
    for kind, url in ENDPOINTS.items():
        try:
            raw = fetcher.get(url, name=f"corp_{kind}.json", retries=2)
            records = json.loads(decode(raw))
            if kind == "insider":
                if not isinstance(records, list):
                    raise ValueError(f"insider 回應不是陣列：{json.dumps(records, ensure_ascii=False)[:80]}")
                save_insider_day(records, insider_out_date(records, D.isoformat()), insider_dir)
            else:
                res[kind] = parse_corporate(records, kind, universe)
            res["sources"].append({"kind": kind, "url": url})
        except Exception as e:  # noqa: BLE001
            warns.append(f"上市{LABELS[kind]}抓取失敗（{type(e).__name__}: {str(e).splitlines()[0][:80]}），該類缺值")
    res["insider"], days = load_insider_window(D.isoformat(), insider_dir, universe)
    res["insider_days"] = days
    if len(days) < INSIDER_DAYS:
        info.append(f"內部人申報累積 {len(days)} 天（{', '.join(days) or '無'}），不足 {INSIDER_DAYS} 個交易日，以現有天數判斷")
    if days and "insider" not in {s_["kind"] for s_ in res["sources"]}:
        res["sources"].append({"kind": "insider", "url": ENDPOINTS["insider"]})
    return res, warns, info


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
