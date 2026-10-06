"""主動型 ETF 前 10 大持股（Active Share 比對基準）。

資料來源：各投信官網／TWSE ETF 揭露頁。每家頁面格式不同，這裡不寫死各投信的版面，而是：
  - `data/reference/etf_sources.csv` 登錄每檔 ETF 的 url 與 format（html / json / csv），由使用者填寫；
  - 通用 parser（HTML 表格、JSON、CSV）依欄位關鍵字（代號/名稱/比重…）找出持股列，取權重前 10 大；
  - 抓不到或解析失敗的 ETF 不會輸出殘缺資料，而是列入 missing 並產生手動補資料的 CSV 模板。
輸出：data/etf_top10/YYYY-MM-DD.json，格式 {etf_code: {ticker: weight}}（weight 為 0~1 的小數）。
海外型 ETF（etf_sources.csv 的 overseas=1）持股與台股無交集，標記並跳過。
"""
from __future__ import annotations

import csv
import json
import re
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import requests

from ..active_share import check_active_share, top_n
from ..config import ACTIVE_SHARE_MIN, ACTIVE_SHARE_TOP_N, REFERENCE_DIR, ROOT
from ..universe import load_active_etfs, load_universe

ETF_TOP10_DIR = ROOT / "data" / "etf_top10"
SOURCES_PATH = REFERENCE_DIR / "etf_sources.csv"
MANUAL_FILENAME = "manual.csv"
TEMPLATE_FILENAME = "manual_template.csv"
MANUAL_COLUMNS = ["etf_code", "name", "rank", "ticker", "weight_pct"]

CODE_KEYS = ("證券代號", "股票代號", "代號", "代碼", "code", "symbol", "ticker", "stockno")
NAME_KEYS = ("證券名稱", "股票名稱", "名稱", "name")
WEIGHT_KEYS = ("比重", "權重", "比例", "weight", "ratio", "percent", "%", "占")


# --------------------------------------------------------------------------- 欄位 / 數值正規化

def _has(key: str, kws) -> bool:
    k = str(key).lower().replace(" ", "")
    return any(w.lower() in k for w in kws)


def parse_ticker(cell) -> str | None:
    """'2330'、'2330 台積電'、'2330.TW'、'2330 TT' → '2330'；無代號（現金、期貨）回傳 None。"""
    m = re.match(r"^\s*(\d{4,6}[A-Z]?)(?![0-9A-Za-z])", str(cell or "").upper())
    return m.group(1) if m else None


def parse_weight(cell) -> tuple[float, bool] | None:
    """回傳 (數值, 是否帶 % 符號)；無法解析回傳 None。"""
    s = str(cell if cell is not None else "").strip().replace(",", "")
    if not s or s in ("-", "--", "N/A"):
        return None
    pct = s.endswith("%")
    try:
        return float(s.rstrip("%").strip()), pct
    except ValueError:
        return None


def _name_to_ticker() -> dict[str, str]:
    return {re.sub(r"\s", "", v["name"]): t for t, v in load_universe().items()}


def normalize_rows(rows: list[dict], what: str = "") -> dict[str, float]:
    """通用持股列 → {ticker: weight(小數)}，取權重前 10 大。

    rows 為 dict 列表，欄名由關鍵字判定。代號欄解析不出來時，改用名稱對 150 檔名單反查；
    仍對不上的列（現金、期貨、名單外且無代號者）略過。有效列少於 10 → ValueError（不輸出殘缺資料）。
    權重單位：任何一格帶 % 或總和 > 1.5 → 視為百分比；否則視為小數。
    """
    if not rows:
        raise ValueError(f"{what}: 沒有任何持股列")
    keys = list(rows[0].keys())
    code_k = next((k for k in keys if _has(k, CODE_KEYS)), None)
    name_k = next((k for k in keys if _has(k, NAME_KEYS)), None)
    w_k = next((k for k in keys if _has(k, WEIGHT_KEYS)), None)
    if w_k is None or (code_k is None and name_k is None):
        raise ValueError(f"{what}: 找不到代號/名稱或權重欄位，現有欄位 {keys}")
    n2t = _name_to_ticker()
    parsed, any_pct = {}, False
    for r in rows:
        t = parse_ticker(r.get(code_k)) if code_k else None
        if t is None and name_k:
            t = n2t.get(re.sub(r"\s", "", str(r.get(name_k, ""))))
        w = parse_weight(r.get(w_k))
        if t is None or w is None:
            continue
        any_pct |= w[1]
        parsed[t] = parsed.get(t, 0.0) + w[0]
    if len(parsed) < ACTIVE_SHARE_TOP_N:
        raise ValueError(f"{what}: 只解析出 {len(parsed)} 檔有代號的持股（需要至少 {ACTIVE_SHARE_TOP_N}）")
    scale = 100.0 if (any_pct or sum(parsed.values()) > 1.5) else 1.0
    out = top_n({t: w / scale for t, w in parsed.items()})
    total = sum(out.values())
    if not 0.05 <= total <= 1.0001:
        raise ValueError(f"{what}: 前 10 大權重總和 {total:.4f} 不合理，可能單位判斷錯誤")
    return {t: round(w, 6) for t, w in out.items()}


# --------------------------------------------------------------------------- parsers

class _TableParser(HTMLParser):
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


def parse_html(html: str, what: str = "") -> dict[str, float]:
    """從 HTML 找出第一個「表頭同時有（代號或名稱）與權重」的表格。巢狀表格只取最外層。"""
    p = _TableParser()
    p.feed(html)
    for table in p.tables:
        for i, header in enumerate(table):
            if any(_has(c, WEIGHT_KEYS) for c in header) and \
                    any(_has(c, CODE_KEYS + NAME_KEYS) for c in header):
                body = [dict(zip(header, r)) for r in table[i + 1:] if len(r) >= len(header)]
                return normalize_rows(body, what)
    raise ValueError(f"{what}: HTML 中找不到持股表格（{len(p.tables)} 個表格皆無代號/權重表頭）")


def _find_record_list(obj):
    """遞迴找出「dict 列表且含代號/名稱與權重 key」的列表（取最長者）。"""
    best = []
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            ks = list(obj[0].keys())
            if any(_has(k, WEIGHT_KEYS) for k in ks) and any(_has(k, CODE_KEYS + NAME_KEYS) for k in ks):
                best = obj
        for x in obj:
            c = _find_record_list(x)
            if len(c) > len(best):
                best = c
    elif isinstance(obj, dict):
        for v in obj.values():
            c = _find_record_list(v)
            if len(c) > len(best):
                best = c
    return best


def parse_json(payload, what: str = "") -> dict[str, float]:
    rows = _find_record_list(payload)
    if not rows:
        raise ValueError(f"{what}: JSON 中找不到含代號與權重的持股列表")
    return normalize_rows(rows, what)


def parse_csv_text(text: str, what: str = "") -> dict[str, float]:
    text = text.lstrip("﻿")
    return normalize_rows(list(csv.DictReader(text.splitlines())), what)


PARSERS = {"html": lambda body, what: parse_html(body, what),
           "json": lambda body, what: parse_json(json.loads(body), what),
           "csv": lambda body, what: parse_csv_text(body, what)}


# --------------------------------------------------------------------------- 來源登錄

def load_sources(path: Path | None = None) -> dict[str, dict]:
    """{etf_code: {issuer, overseas(bool), format, url}}；以 active_etfs.csv 的 30 檔為準。"""
    out = {c: {"issuer": "", "overseas": False, "format": "", "url": ""} for c in load_active_etfs()}
    with open(path or SOURCES_PATH, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["etf_code"] in out:
                out[r["etf_code"]] = {"issuer": r["issuer"], "overseas": r["overseas"].strip() == "1",
                                      "format": r["format"].strip().lower(), "url": r["url"].strip()}
    return out


# --------------------------------------------------------------------------- 抓取

def fetch_one(code: str, src: dict, session: requests.Session, raw_dir: Path | None, day: str,
              timeout: float = 30) -> dict[str, float]:
    if not src["url"] or src["format"] not in PARSERS:
        raise ValueError("etf_sources.csv 尚未填 url/format")
    resp = session.get(src["url"], timeout=timeout)
    resp.raise_for_status()
    if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    body = resp.text
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"etf_{code}_{day}.{src['format']}").write_text(body, encoding="utf-8")
    return PARSERS[src["format"]](body, code)


def fetch_all(day: date | str, raw_dir: Path | None = None, sources: dict[str, dict] | None = None,
              session: requests.Session | None = None) -> dict:
    """逐檔抓取；單檔失敗不影響其他檔。回傳 {date, top10, overseas, missing:{etf: 原因}}。"""
    ds = str(day)
    sources = sources if sources is not None else load_sources()
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", "Mozilla/5.0 (esun-etf-agent)")
    top10, overseas, missing = {}, [], {}
    for code, src in sources.items():
        if src["overseas"]:
            overseas.append(code)
            continue
        try:
            top10[code] = fetch_one(code, src, session, raw_dir, ds)
        except Exception as e:  # 每檔獨立：網路、HTTP、格式改版都只標記該檔
            missing[code] = f"{type(e).__name__}: {e}"
    return {"date": ds, "top10": top10, "overseas": sorted(overseas), "missing": missing}


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
            w = parse_weight(r.get("weight_pct"))
            if w is None:
                raise ValueError(f"{path.name}: {code} {t} 的 weight_pct 無法解析: {r.get('weight_pct')!r}")
            if t in rows.setdefault(code, {}):
                raise ValueError(f"{path.name}: {code} 的 {t} 重複")
            rows[code][t] = w[0] / 100.0
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
           sources: dict[str, dict] | None = None, session: requests.Session | None = None) -> dict:
    """抓取 + 合併手動檔（base/manual.csv，若存在）+ 存檔；缺漏者產生 base/manual_template.csv。
    回傳 fetch_all 的結果，並加上 manual（由手動檔補上的 ETF）；missing 為補完後仍缺的。"""
    base = base or ETF_TOP10_DIR
    res = fetch_all(day, raw_dir, sources, session)
    manual_path = base / MANUAL_FILENAME
    manual = load_manual(manual_path) if manual_path.exists() else {}
    res["manual"] = sorted(manual)
    for code, h in manual.items():
        if code in res["overseas"]:
            continue
        res["top10"][code] = h
        res["missing"].pop(code, None)
    p = top10_path(res["date"], base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(res["top10"], ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    if res["missing"]:
        res["template"] = str(write_manual_template(sorted(res["missing"]), base / TEMPLATE_FILENAME))
    return res


def load_top10(day: date | str | None = None, base: Path | None = None) -> dict[str, dict[str, float]]:
    """讀取 {etf_code: {ticker: weight}}；day=None 取最新一份。手動檔（manual.csv）會覆蓋同代號。"""
    base = base or ETF_TOP10_DIR
    if day is None:
        files = sorted(base.glob("????-??-??.json"))
        if not files:
            raise FileNotFoundError(f"{base} 下沒有 ETF 前 10 大資料")
        p = files[-1]
    else:
        p = top10_path(day, base)
    data = json.loads(p.read_text(encoding="utf-8"))
    mp = base / MANUAL_FILENAME
    if mp.exists():
        data.update(load_manual(mp))
    return data


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
