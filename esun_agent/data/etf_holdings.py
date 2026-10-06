"""主動型 ETF 前 10 大持股（Active Share 比對基準）。

單一資料源：MoneyDJ「持股明細」頁
  https://www.moneydj.com/ETF/X/Basic/Basic0007.xdjhtm?etfid={code}.TW
頁面為 UTF-8（requests 必須設 resp.encoding="utf-8"）。「持股明細」區塊有「資料日期：YYYY/MM/DD」，
接著是表格「個股名稱｜投資比例(%)｜持有股數」，列出前 10 大。個股名稱格式：
  台積電(2330.TW)、國巨*(2327.TW)、Lumentum(LITE.US)、Samsung Elec Mech(009150.KS)
.TW 且 4 位數 → 台股代號（如 "2330"）；其他保留括號內原字串（如 "LITE.US"），不視為台股。

輸出 data/etf_top10/YYYY-MM-DD.json：
  {"as_of": 抓取基準日, "data_dates": {etf: 該檔資料日期}, "top10": {etf: {ticker: weight(0~1)}},
   "overseas": [純美股、跳過者], "stale": {etf: 落後天數}}
每檔 ETF 的資料日期可能不同；比基準日（最新交易日）舊超過 STALE_DAYS 天會列入 stale 警告（不視為缺漏）。
抓不到或解析失敗的 ETF 不輸出殘缺資料，列入 missing 並產生手動補資料的 CSV 模板。
"""
from __future__ import annotations

import csv
import json
import re
import time
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

import requests

from ..active_share import check_active_share, top_n
from ..config import ACTIVE_SHARE_MIN, ACTIVE_SHARE_TOP_N, REFERENCE_DIR, ROOT
from ..universe import load_active_etfs

ETF_TOP10_DIR = ROOT / "data" / "etf_top10"
SOURCES_PATH = REFERENCE_DIR / "etf_sources.csv"
MARKET_DIR = ROOT / "data" / "market"
MANUAL_FILENAME = "manual.csv"
TEMPLATE_FILENAME = "manual_template.csv"
MANUAL_COLUMNS = ["etf_code", "name", "rank", "ticker", "weight_pct"]
MONEYDJ_URL = "https://www.moneydj.com/ETF/X/Basic/Basic0007.xdjhtm?etfid={code}.TW"
STALE_DAYS = 3
MANUAL_DATA_DATE = "manual"


# --------------------------------------------------------------------------- 解析

def parse_holding_name(cell: str) -> str | None:
    """'台積電(2330.TW)' → '2330'；'Lumentum(LITE.US)' → 'LITE.US'；'009150.KS' 同理保留原字串。
    .TW 但不是 4 位數（如 00679B.TW）也保留原字串。沒有括號代號（如「英飛凌科技股份有限公司」）
    → 用完整名稱當識別字（視為非台股），不丟棄該列。名稱為空回傳 None。"""
    name = re.sub(r"\s+", " ", str(cell or "")).strip()
    if not name:
        return None
    m = re.search(r"\(([^()\s]+)\)\s*$", name)
    if not m:
        return name
    code = m.group(1).upper()
    m2 = re.fullmatch(r"(\d{4})\.TW", code)
    return m2.group(1) if m2 else code


def parse_percent(cell) -> float | None:
    s = str(cell if cell is not None else "").strip().replace(",", "").rstrip("%").strip()
    try:
        return float(s)
    except ValueError:
        return None


class _TableParser(HTMLParser):
    """收集所有表格（只取最外層）的儲存格文字；cell 內巢狀標籤（a/span）的文字會合併。"""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self.tables.append([])
        elif self._depth and tag == "tr":
            self._row = []
        elif self._depth and tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "table":
            self._depth = max(0, self._depth - 1)
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row and self.tables:
                self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


_DATE_RE = re.compile(r"資料日期\s*[：:]\s*(?:<[^>]*>\s*)*(\d{4})/(\d{1,2})/(\d{1,2})")


def parse_moneydj(html: str, what: str = "") -> dict:
    """解析 MoneyDJ 持股明細頁 → {"data_date": "YYYY-MM-DD", "holdings": {ticker: weight(小數)}}。

    找「持股明細」之後的每個「資料日期」，若其後緊接的第一個表格表頭含「個股名稱」與「投資比例」，即為持股表。
    有效列（名稱非空且比例可解析）少於 10 → ValueError；多於 10 取權重前 10 大。
    """
    marker = html.find("持股明細")
    if marker < 0:
        raise ValueError(f"{what}: 頁面沒有「持股明細」區塊")
    last_err = "找不到「資料日期」"
    for m in _DATE_RE.finditer(html, marker):
        p = _TableParser()
        p.feed(html[m.end():])
        for table in p.tables[:1]:  # 只看「資料日期」後緊接的第一個表格
            hdr_i = next((i for i, r in enumerate(table)
                          if any("個股名稱" in c for c in r) and any("投資比例" in c for c in r)), None)
            if hdr_i is None:
                continue
            hdr = table[hdr_i]
            ni = next(i for i, c in enumerate(hdr) if "個股名稱" in c)
            wi = next(i for i, c in enumerate(hdr) if "投資比例" in c)
            holdings: dict[str, float] = {}
            for r in table[hdr_i + 1:]:
                if len(r) <= max(ni, wi):
                    continue
                t, w = parse_holding_name(r[ni]), parse_percent(r[wi])
                if t is None or w is None:
                    continue
                holdings[t] = holdings.get(t, 0.0) + w / 100.0
            if len(holdings) < ACTIVE_SHARE_TOP_N:
                last_err = f"只解析出 {len(holdings)} 檔持股（需要至少 {ACTIVE_SHARE_TOP_N}）"
                break
            out = {t: round(w, 6) for t, w in top_n(holdings).items()}
            if not 0.05 <= sum(out.values()) <= 1.0001:
                raise ValueError(f"{what}: 前 10 大權重總和 {sum(out.values()):.4f} 不合理")
            y, mo, d = (int(x) for x in m.groups())
            return {"data_date": date(y, mo, d).isoformat(), "holdings": out}
        # 這個「資料日期」後面沒有持股表 → 試下一個
    raise ValueError(f"{what}: 無法解析持股明細：{last_err}")


# --------------------------------------------------------------------------- 來源登錄

def load_sources(path: Path | None = None) -> dict[str, dict]:
    """{etf_code: {overseas(bool), url}}；以 active_etfs.csv 的 30 檔為準。
    overseas=1 只用在純美股 ETF（持股與台股無交集）；全球型但持有台股者不可跳過。"""
    out = {c: {"overseas": False, "url": MONEYDJ_URL.format(code=c)} for c in load_active_etfs()}
    with open(path or SOURCES_PATH, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["etf_code"] in out:
                out[r["etf_code"]] = {"overseas": r["overseas"].strip() == "1",
                                      "url": r["url"].strip() or MONEYDJ_URL.format(code=r["etf_code"])}
    return out


# --------------------------------------------------------------------------- 抓取

def latest_trading_day(market_dir: Path | None = None) -> date | None:
    """data/market/ 下最新的行情檔日期；沒有則 None。"""
    files = sorted((market_dir or MARKET_DIR).glob("????-??-??.parquet"))
    return date.fromisoformat(files[-1].stem) if files else None


def fetch_one(code: str, src: dict, session: requests.Session, raw_dir: Path | None, day: str,
              timeout: float = 30) -> dict:
    resp = session.get(src["url"], timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"  # MoneyDJ 為 UTF-8，不設會被猜成 ISO-8859-1 而亂碼
    body = resp.text
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"etf_{code}_{day}.html").write_text(body, encoding="utf-8")
    return parse_moneydj(body, code)


def fetch_all(day: date | str, raw_dir: Path | None = None, sources: dict[str, dict] | None = None,
              session: requests.Session | None = None, ref_date: date | None = None,
              sleep: float = 0.0) -> dict:
    """逐檔抓取；單檔失敗不影響其他檔。ref_date = 最新交易日（stale 判斷基準；預設取 data/market 最新檔，
    再沒有就用 day）。回傳 {date, top10, data_dates, overseas, missing:{etf: 原因}, stale:{etf: 落後天數}}。"""
    ds = str(day)
    ref = ref_date or latest_trading_day() or date.fromisoformat(ds)
    sources = sources if sources is not None else load_sources()
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", "Mozilla/5.0 (esun-etf-agent)")
    top10, data_dates, overseas, missing, stale = {}, {}, [], {}, {}
    for code, src in sources.items():
        if src["overseas"]:
            overseas.append(code)
            continue
        if sleep:
            time.sleep(sleep)
        try:
            r = fetch_one(code, src, session, raw_dir, ds)
        except Exception as e:  # 每檔獨立：網路、HTTP、格式改版都只標記該檔
            missing[code] = f"{type(e).__name__}: {e}"
            continue
        top10[code], data_dates[code] = r["holdings"], r["data_date"]
        lag = (ref - date.fromisoformat(r["data_date"])).days
        if lag > STALE_DAYS:
            stale[code] = lag
    return {"date": ds, "ref_date": ref.isoformat(), "top10": top10, "data_dates": data_dates,
            "overseas": sorted(overseas), "missing": missing, "stale": stale}


# --------------------------------------------------------------------------- 手動補資料

def write_manual_template(codes: list[str], path: Path) -> Path:
    """每個缺漏的 ETF 產生 10 列空白（rank 1~10），使用者填 ticker 與 weight_pct（百分比，如 8.52）。"""
    names = load_active_etfs()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(MANUAL_COLUMNS)
        for c in codes:
            for rank in range(1, ACTIVE_SHARE_TOP_N + 1):
                w.writerow([c, names.get(c, ""), rank, "", ""])
    return path


def load_manual(path: Path) -> dict[str, dict[str, float]]:
    """讀取手動補的 CSV → {etf_code: {ticker: weight}}。ticker 空白的列略過；
    某 ETF 填了但不足 10 檔、代號重複或不在 active_etfs 名單 → ValueError。"""
    known = load_active_etfs()
    rows: dict[str, dict[str, float]] = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            t = (r.get("ticker") or "").strip()
            if not t:
                continue
            code = r["etf_code"].strip()
            if code not in known:
                raise ValueError(f"{path.name}: 未知的 ETF 代號 {code}")
            w = parse_percent(r.get("weight_pct"))
            if w is None:
                raise ValueError(f"{path.name}: {code} {t} 的 weight_pct 無法解析: {r.get('weight_pct')!r}")
            if t in rows.setdefault(code, {}):
                raise ValueError(f"{path.name}: {code} 的 {t} 重複")
            rows[code][t] = w / 100.0
    for code, h in rows.items():
        if len(h) < ACTIVE_SHARE_TOP_N:
            raise ValueError(f"{path.name}: {code} 只填了 {len(h)} 檔，需要 {ACTIVE_SHARE_TOP_N} 檔")
        if not 0.05 <= sum(h.values()) <= 1.0001:
            raise ValueError(f"{path.name}: {code} 權重總和 {sum(h.values()):.4f} 不合理（weight_pct 要填百分比）")
    return {c: {t: round(w, 6) for t, w in top_n(h).items()} for c, h in rows.items()}


# --------------------------------------------------------------------------- 存取

def top10_path(day: date | str, base: Path | None = None) -> Path:
    return (base or ETF_TOP10_DIR) / f"{day}.json"


def update(day: date | str, base: Path | None = None, raw_dir: Path | None = None,
           sources: dict[str, dict] | None = None, session: requests.Session | None = None,
           ref_date: date | None = None, sleep: float = 0.0) -> dict:
    """抓取 + 合併手動檔（base/manual.csv，若存在）+ 存檔；缺漏者產生 base/manual_template.csv。
    手動補的 ETF 資料日期記為 "manual"（無法驗證新舊，請自行確認）。"""
    base = base or ETF_TOP10_DIR
    res = fetch_all(day, raw_dir, sources, session, ref_date, sleep)
    manual_path = base / MANUAL_FILENAME
    manual = load_manual(manual_path) if manual_path.exists() else {}
    res["manual"] = sorted(c for c in manual if c not in res["overseas"])
    for code in res["manual"]:
        res["top10"][code] = manual[code]
        res["data_dates"][code] = MANUAL_DATA_DATE
        res["missing"].pop(code, None)
        res["stale"].pop(code, None)
    out = {"as_of": res["date"], "ref_date": res["ref_date"], "data_dates": res["data_dates"],
           "top10": res["top10"], "overseas": res["overseas"], "stale": res["stale"]}
    p = top10_path(res["date"], base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    if res["missing"]:
        res["template"] = str(write_manual_template(sorted(res["missing"]), base / TEMPLATE_FILENAME))
    return res


def _read(day: date | str | None, base: Path) -> dict:
    if day is None:
        files = sorted(base.glob("????-??-??.json"))
        if not files:
            raise FileNotFoundError(f"{base} 下沒有 ETF 前 10 大資料")
        p = files[-1]
    else:
        p = top10_path(day, base)
    return json.loads(p.read_text(encoding="utf-8"))


def load_top10(day: date | str | None = None, base: Path | None = None) -> dict[str, dict[str, float]]:
    """讀取 {etf_code: {ticker: weight}}；day=None 取最新一份。手動檔（manual.csv）會覆蓋同代號。"""
    base = base or ETF_TOP10_DIR
    data = _read(day, base)["top10"]
    mp = base / MANUAL_FILENAME
    if mp.exists():
        data.update(load_manual(mp))
    return data


def load_data_dates(day: date | str | None = None, base: Path | None = None) -> dict[str, str]:
    """{etf_code: 資料日期 'YYYY-MM-DD' 或 'manual'}。"""
    return _read(day, base or ETF_TOP10_DIR)["data_dates"]


# --------------------------------------------------------------------------- Active Share 串接

def check_target_weights(weights: dict[str, float], etf_top10: dict[str, dict[str, float]] | None = None,
                         margin: float = 0.02, expected_etfs: list[str] | None = None) -> dict:
    """擬定的目標權重（{ticker: 占 NAV 小數}）是否通過 Active Share。

    - ok：以官方門檻 ACTIVE_SHARE_MIN 判斷（兩種權重口徑取較小者，同 check_active_share）。
    - safe：再加 margin 緩衝（ETF 揭露有時間差，建議以 safe 為準）。
    - fails：每檔未通過（< 門檻）的 ETF 與調整建議；warnings：未達 safe 但仍 ≥ 門檻的 ETF。
    - missing：expected_etfs（預設 = 資料中缺少的非海外主動型 ETF）沒有資料者；有缺漏時 complete=False，
      此時 ok 只代表「已有資料的 ETF」都通過，不能當成全數通過。
    調整建議（heuristic）：Active Share（歸一化）= 1 − Σmin(p_i, b_i)，列出與該 ETF 前 10 大重疊的標的，
    依重疊量排序；needed_cut 是歸一化口徑下重疊量需再下降的幅度。實際調整後請重跑本函式確認。
    """
    etf_top10 = etf_top10 if etf_top10 is not None else load_top10()
    res = check_active_share(weights, etf_top10, ACTIVE_SHARE_MIN)
    if expected_etfs is None:
        src = load_sources()
        expected_etfs = [c for c, s in src.items() if not s["overseas"]]
    missing = sorted(set(expected_etfs) - set(etf_top10))
    fails, warnings = [], []
    ptop = top_n(weights)
    ptot = sum(ptop.values()) or 1.0
    for etf, h in etf_top10.items():
        d = res["detail"][etf]
        low = min(d["normalized"], d["raw"])
        if low >= ACTIVE_SHARE_MIN + margin:
            continue
        btop = top_n(h)
        btot = sum(btop.values()) or 1.0
        overlap = sorted(
            ({"ticker": t, "portfolio_weight": ptop[t], "etf_weight": btop[t],
              "overlap": round(min(ptop[t] / ptot, btop[t] / btot), 6)} for t in set(ptop) & set(btop)),
            key=lambda x: -x["overlap"])
        item = {"etf": etf, "active_share": d, "min": low, "overlap_tickers": overlap}
        if low < ACTIVE_SHARE_MIN:
            item["needed_cut"] = round(ACTIVE_SHARE_MIN - low, 6)
            fails.append(item)
        else:
            warnings.append(item)
    return {"ok": res["ok"], "safe": not fails and not warnings, "complete": not missing,
            "min": res["min"], "worst_etf": res["worst_etf"], "fails": fails, "warnings": warnings,
            "missing": missing, "margin": margin}
