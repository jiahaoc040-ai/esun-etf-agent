"""上市櫃日行情（含成交均價）與三大法人買賣超。

來源（皆為官方，D-Plan sources.authority：上市 twse、上櫃 tpex）：
- TWSE OpenAPI STOCK_DAY_ALL（最新交易日全市場）、exchangeReport/STOCK_DAY（個股依月份）
- TPEx OpenAPI tpex_mainboard_daily_close_quotes（最新交易日）、daily_close_quotes（依日期）
- TWSE fund/T86、TPEx tpex_3insti_daily_trading（三大法人）

parser 皆為純函式（不碰網路），以 tests/fixtures/market/ 的假資料測試。
抓取函式只做 HTTP + 呼叫 parser。只保留 universe_150 的 150 檔。
"""
from __future__ import annotations

import math
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from ..config import ROOT
from ..universe import authority_for, load_universe

MARKET_DIR = ROOT / "data" / "market"
INSTITUTIONAL_DIR = ROOT / "data" / "institutional"

MARKET_COLUMNS = ["date", "ticker", "open", "high", "low", "close", "volume", "value", "avg_price"]
INST_COLUMNS = ["date", "ticker", "foreign_net", "trust_net", "dealer_net", "total_net"]

TWSE_DAY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_STOCK_DAY_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TWSE_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_DAY_ALL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TPEX_DAY_URL = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php"
TPEX_INST_ALL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
TPEX_INST_URL = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"

CLOSE_TIME = "13:30:00+08:00"  # 收盤資料的 content_as_of；若交易所另有公告時間，呼叫端可覆寫
TZ_SUFFIX = "+08:00"


# --------------------------------------------------------------------------- 基本工具

def to_number(x) -> float:
    """'1,234.5' → 1234.5；'--'、'', None、'X0.00' 之類無法解析者回傳 NaN。"""
    if x is None:
        return math.nan
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(",", "")
    if s in ("", "--", "---", "-", "N/A"):
        return math.nan
    try:
        return float(s)
    except ValueError:
        return math.nan


def parse_roc_date(s: str) -> date:
    """'1141231'、'114/12/31'、'114-12-31' → date（民國年）。西元 8 碼 '20251231' 也接受。"""
    s = str(s).strip()
    m = re.fullmatch(r"(\d{2,3})[/\-.]?(\d{2})[/\-.]?(\d{2})", s)
    if m:
        return date(int(m[1]) + 1911, int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{4})[/\-.]?(\d{2})[/\-.]?(\d{2})", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    raise ValueError(f"無法解析日期: {s!r}")


def to_roc_slash(d: date) -> str:
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def _finish(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=columns)
    if df.empty:
        return df
    return df.sort_values(["date", "ticker"]).reset_index(drop=True)


def _make_row(d: date, ticker: str, o, h, l, c, volume, value) -> dict | None:
    """收盤價或成交量無效（停牌、無成交）的列回傳 None。"""
    close, vol, val = to_number(c), to_number(volume), to_number(value)
    if math.isnan(close) or math.isnan(vol) or vol <= 0 or math.isnan(val):
        return None
    return {
        "date": d.isoformat(), "ticker": ticker,
        "open": to_number(o), "high": to_number(h), "low": to_number(l), "close": close,
        "volume": int(vol), "value": int(val), "avg_price": val / vol,
    }


# --------------------------------------------------------------------------- parsers：行情

def parse_twse_day_all(records: list[dict], universe: dict | None = None) -> pd.DataFrame:
    """TWSE OpenAPI STOCK_DAY_ALL。欄位：Date, Code, TradeVolume, TradeValue,
    OpeningPrice, HighestPrice, LowestPrice, ClosingPrice。"""
    uni = universe if universe is not None else load_universe()
    rows = []
    for r in records:
        code = str(r.get("Code", "")).strip()
        if code not in uni or uni[code]["market"] != "TWSE":
            continue
        row = _make_row(parse_roc_date(r["Date"]), code, r.get("OpeningPrice"), r.get("HighestPrice"),
                        r.get("LowestPrice"), r.get("ClosingPrice"), r.get("TradeVolume"), r.get("TradeValue"))
        if row:
            rows.append(row)
    return _finish(rows, MARKET_COLUMNS)


def parse_twse_stock_day(payload: dict, ticker: str) -> pd.DataFrame:
    """TWSE exchangeReport/STOCK_DAY（單檔、整月）。
    fields: 日期, 成交股數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, 漲跌價差, 成交筆數。
    stat 不是 OK（例如「很抱歉，沒有符合條件的資料!」）→ 空表。"""
    if payload.get("stat") != "OK":
        return pd.DataFrame(columns=MARKET_COLUMNS)
    idx = {name: i for i, name in enumerate(payload["fields"])}
    rows = []
    for r in payload.get("data", []):
        row = _make_row(parse_roc_date(r[idx["日期"]]), ticker, r[idx["開盤價"]], r[idx["最高價"]],
                        r[idx["最低價"]], r[idx["收盤價"]], r[idx["成交股數"]], r[idx["成交金額"]])
        if row:
            rows.append(row)
    return _finish(rows, MARKET_COLUMNS)


def parse_tpex_day_all(records: list[dict], universe: dict | None = None) -> pd.DataFrame:
    """TPEx OpenAPI tpex_mainboard_daily_close_quotes。欄位：Date, SecuritiesCompanyCode,
    Close, Open, High, Low, TradingShares, TransactionAmount。"""
    uni = universe if universe is not None else load_universe()
    rows = []
    for r in records:
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        if code not in uni or uni[code]["market"] != "TPEX":
            continue
        row = _make_row(parse_roc_date(r["Date"]), code, r.get("Open"), r.get("High"), r.get("Low"),
                        r.get("Close"), r.get("TradingShares"), r.get("TransactionAmount"))
        if row:
            rows.append(row)
    return _finish(rows, MARKET_COLUMNS)


def _find_col(fields: list[str], *keywords: str) -> int:
    """回傳第一個同時含所有 keywords 的欄位索引；找不到丟 KeyError（欄位改版時要大聲失敗）。"""
    for i, name in enumerate(fields):
        if all(k in name for k in keywords):
            return i
    raise KeyError(f"找不到欄位 {keywords}，現有欄位: {fields}")


def parse_tpex_history_day(payload: dict, d: date, universe: dict | None = None) -> pd.DataFrame:
    """TPEx 依日期查詢（daily_close_quotes，o=json）。tables[0] 有 fields 與 data。
    欄位：代號, 名稱, 收盤, 漲跌, 開盤, 最高, 最低, 均價, 成交股數, 成交金額(元), ..."""
    uni = universe if universe is not None else load_universe()
    tables = payload.get("tables") or []
    if not tables or not tables[0].get("data"):
        return pd.DataFrame(columns=MARKET_COLUMNS)
    t = tables[0]
    f = t["fields"]
    ic, ico, ih, il, icl = (_find_col(f, "代號"), _find_col(f, "開盤"), _find_col(f, "最高"),
                            _find_col(f, "最低"), _find_col(f, "收盤"))
    iv, iva = _find_col(f, "成交股數"), _find_col(f, "成交金額")
    rows = []
    for r in t["data"]:
        code = str(r[ic]).strip()
        if code not in uni or uni[code]["market"] != "TPEX":
            continue
        row = _make_row(d, code, r[ico], r[ih], r[il], r[icl], r[iv], r[iva])
        if row:
            rows.append(row)
    return _finish(rows, MARKET_COLUMNS)


# --------------------------------------------------------------------------- parsers：三大法人
# 欄位淨買賣超以「股數」為單位。

def parse_twse_t86(payload: dict, d: date, universe: dict | None = None) -> pd.DataFrame:
    """TWSE fund/T86（selectType=ALLBUT0999 或 ALL）。外資 = 外陸資(不含外資自營商) + 外資自營商。"""
    uni = universe if universe is not None else load_universe()
    if payload.get("stat") != "OK":
        return pd.DataFrame(columns=INST_COLUMNS)
    f = payload["fields"]
    ic = _find_col(f, "證券代號")
    i_for = _find_col(f, "外陸資買賣超股數", "不含外資自營商")
    i_trust = _find_col(f, "投信買賣超股數")
    i_dealer = _find_col(f, "自營商買賣超股數")
    # 「自營商買賣超股數」會同時匹配 (自行買賣)/(避險) 子欄位，取不含括號者
    for i, name in enumerate(f):
        if name == "自營商買賣超股數":
            i_dealer = i
    i_total = _find_col(f, "三大法人買賣超股數")
    try:
        i_fdealer = _find_col(f, "外資自營商買賣超股數")
    except KeyError:
        i_fdealer = None
    rows = []
    for r in payload["data"]:
        code = str(r[ic]).strip()
        if code not in uni or uni[code]["market"] != "TWSE":
            continue
        foreign = to_number(r[i_for])
        if i_fdealer is not None:
            fd = to_number(r[i_fdealer])
            foreign = foreign + (0 if math.isnan(fd) else fd)
        rows.append({"date": d.isoformat(), "ticker": code, "foreign_net": foreign,
                     "trust_net": to_number(r[i_trust]), "dealer_net": to_number(r[i_dealer]),
                     "total_net": to_number(r[i_total])})
    return _finish(rows, INST_COLUMNS)


def _norm(k: str) -> str:
    return re.sub(r"[^a-z]", "", k.lower())


def parse_tpex_inst_all(records: list[dict], universe: dict | None = None) -> pd.DataFrame:
    """TPEx OpenAPI tpex_3insti_daily_trading（最新交易日）。欄位為英文長名，正規化後以關鍵字比對
    各「…Difference」（買賣超）欄；找不到欄位會丟 KeyError，避免欄位改版後悄悄產出 NaN。
    注意：此 API 的實際欄名未在雲端環境驗證，首次在本機/Actions 執行時請核對。"""
    uni = universe if universe is not None else load_universe()

    def pick(r: dict, pred) -> float:
        for k, v in r.items():
            n = _norm(k)
            if "difference" in n and pred(n):
                return to_number(v)
        raise KeyError(f"TPEx 三大法人找不到欄位，現有欄位: {list(r)}")

    rows = []
    for r in records:
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        if code not in uni or uni[code]["market"] != "TPEX":
            continue
        rows.append({
            "date": parse_roc_date(r["Date"]).isoformat(), "ticker": code,
            "foreign_net": pick(r, lambda n: "foreign" in n and "mainland" in n and "excluded" not in n),
            "trust_net": pick(r, lambda n: "trust" in n),
            "dealer_net": pick(r, lambda n: "dealer" in n and not any(
                x in n for x in ("proprietary", "hedge", "foreign"))),
            "total_net": pick(r, lambda n: "total" in n and not any(
                x in n for x in ("foreign", "trust", "dealer"))),
        })
    return _finish(rows, INST_COLUMNS)


def parse_tpex_inst_history_day(payload: dict, d: date, universe: dict | None = None) -> pd.DataFrame:
    """TPEx 依日期查詢三大法人（3itrade_hedge_result，o=json）。tables[0].fields 的欄位名重複
    （買進/賣出/買賣超 各三組），故用「買賣超」欄位依出現順序：外資及陸資(不含外資自營商)、
    外資自營商、外資及陸資合計、投信、自營商(自行)、自營商(避險)、自營商合計、三大法人合計。"""
    uni = universe if universe is not None else load_universe()
    tables = payload.get("tables") or []
    if not tables or not tables[0].get("data"):
        return pd.DataFrame(columns=INST_COLUMNS)
    t = tables[0]
    f = t["fields"]
    ic = _find_col(f, "代號")
    nets = [i for i, name in enumerate(f) if "買賣超" in name]
    if len(nets) < 8:
        raise KeyError(f"TPEx 三大法人欄位數不符預期（買賣超欄 {len(nets)}）: {f}")
    i_for, i_trust, i_dealer, i_total = nets[2], nets[3], nets[6], nets[7]
    rows = []
    for r in t["data"]:
        code = str(r[ic]).strip()
        if code not in uni or uni[code]["market"] != "TPEX":
            continue
        rows.append({"date": d.isoformat(), "ticker": code, "foreign_net": to_number(r[i_for]),
                     "trust_net": to_number(r[i_trust]), "dealer_net": to_number(r[i_dealer]),
                     "total_net": to_number(r[i_total])})
    return _finish(rows, INST_COLUMNS)


# --------------------------------------------------------------------------- HTTP

class MarketFetcher:
    """薄薄一層 HTTP；sleep 為每次請求前的間隔（TWSE 對高頻請求會封 IP）。"""

    def __init__(self, session: requests.Session | None = None, sleep: float = 0.0,
                 retries: int = 3, timeout: float = 30):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "Mozilla/5.0 (esun-etf-agent)")
        self.sleep, self.retries, self.timeout = sleep, retries, timeout

    def get_json(self, url: str, params: dict | None = None):
        last: Exception | None = None
        for attempt in range(self.retries):
            if self.sleep:
                time.sleep(self.sleep)
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as e:
                last = e
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"GET {url} {params} 失敗: {last}") from last

    # ---- 行情
    def twse_day_all(self) -> pd.DataFrame:
        """最新交易日全市場（OpenAPI 只提供最新一日）。"""
        return parse_twse_day_all(self.get_json(TWSE_DAY_ALL_URL))

    def twse_stock_month(self, ticker: str, year: int, month: int) -> pd.DataFrame:
        payload = self.get_json(TWSE_STOCK_DAY_URL, {"response": "json", "date": f"{year}{month:02d}01",
                                                      "stockNo": ticker})
        return parse_twse_stock_day(payload, ticker)

    def tpex_day_all(self) -> pd.DataFrame:
        return parse_tpex_day_all(self.get_json(TPEX_DAY_ALL_URL))

    def tpex_day(self, d: date) -> pd.DataFrame:
        payload = self.get_json(TPEX_DAY_URL, {"l": "zh-tw", "d": to_roc_slash(d), "o": "json"})
        return parse_tpex_history_day(payload, d)

    # ---- 三大法人
    def twse_inst(self, d: date) -> pd.DataFrame:
        payload = self.get_json(TWSE_T86_URL, {"date": d.strftime("%Y%m%d"), "selectType": "ALLBUT0999",
                                               "response": "json"})
        return parse_twse_t86(payload, d)

    def tpex_inst(self, d: date) -> pd.DataFrame:
        payload = self.get_json(TPEX_INST_URL, {"l": "zh-tw", "se": "EW", "t": "D", "d": to_roc_slash(d),
                                                "o": "json"})
        return parse_tpex_inst_history_day(payload, d)

    def tpex_inst_latest(self) -> pd.DataFrame:
        return parse_tpex_inst_all(self.get_json(TPEX_INST_ALL_URL))


# --------------------------------------------------------------------------- 取得單日 + 儲存

def fetch_latest(fetcher: MarketFetcher | None = None) -> pd.DataFrame:
    """抓最新交易日（上市+上櫃）行情；兩市場日期必須一致，否則丟錯（避免存到半份資料）。"""
    f = fetcher or MarketFetcher()
    df = pd.concat([f.twse_day_all(), f.tpex_day_all()], ignore_index=True)
    if df.empty:
        raise RuntimeError("最新行情為空")
    if df["date"].nunique() != 1:
        raise RuntimeError(f"上市/上櫃日期不一致: {sorted(df['date'].unique())}")
    return df.sort_values("ticker").reset_index(drop=True)


def market_path(d: date | str, base: Path | None = None) -> Path:
    return (base or MARKET_DIR) / f"{d}.parquet"


def institutional_path(d: date | str, base: Path | None = None) -> Path:
    return (base or INSTITUTIONAL_DIR) / f"{d}.parquet"


def save_daily(df: pd.DataFrame, base: Path | None = None, kind: str = "market") -> list[Path]:
    """依 date 欄位拆成每天一份 parquet；回傳寫出的路徑。"""
    path_fn = market_path if kind == "market" else institutional_path
    cols = MARKET_COLUMNS if kind == "market" else INST_COLUMNS
    out = []
    for d, part in df.groupby("date"):
        p = path_fn(d, base)
        p.parent.mkdir(parents=True, exist_ok=True)
        part[cols].sort_values("ticker").reset_index(drop=True).to_parquet(p, index=False)
        out.append(p)
    return out


def load_daily(d: date | str, base: Path | None = None, kind: str = "market") -> pd.DataFrame:
    path_fn = market_path if kind == "market" else institutional_path
    return pd.read_parquet(path_fn(d, base))


def check_coverage(df: pd.DataFrame, universe: dict | None = None) -> list[str]:
    """回傳當日缺少行情的 ticker（停牌或抓取遺漏），供呼叫端警示。"""
    uni = universe if universe is not None else load_universe()
    return sorted(set(uni) - set(df["ticker"]))


# --------------------------------------------------------------------------- D-Plan source 物件

def source_info(d: date | str, authority: str, *, historical: bool = False, as_of: str | None = None,
                kind: str = "market") -> dict:
    """D-Plan source 需要的 url / content_as_of / authority。

    content_as_of：收盤資料描述的時點，預設為當天 13:30+08:00；交易所另有公告時間（如三大法人
    約 16:30 後公布）可用 as_of 傳入完整 ISO 時間覆寫。
    historical=True 回傳指向該日期的查詢 URL，否則為 OpenAPI 的最新日 URL。
    """
    if authority not in ("twse", "tpex"):
        raise ValueError(f"authority 只能是 twse 或 tpex: {authority}")
    ds = d.isoformat() if isinstance(d, date) else str(d)
    dd = date.fromisoformat(ds)
    if kind == "market":
        if authority == "twse":
            url = f"{TWSE_STOCK_DAY_URL}?response=json&date={dd:%Y%m%d}" if historical else TWSE_DAY_ALL_URL
        else:
            url = f"{TPEX_DAY_URL}?l=zh-tw&d={to_roc_slash(dd)}&o=json" if historical else TPEX_DAY_ALL_URL
    elif kind == "institutional":
        if authority == "twse":
            url = f"{TWSE_T86_URL}?date={dd:%Y%m%d}&selectType=ALLBUT0999&response=json"
        else:
            url = f"{TPEX_INST_URL}?l=zh-tw&se=EW&t=D&d={to_roc_slash(dd)}&o=json" if historical \
                else TPEX_INST_ALL_URL
    else:
        raise ValueError(kind)
    return {"authority": authority, "url": url, "content_as_of": as_of or f"{ds}T{CLOSE_TIME}"}


def sources_for_day(df: pd.DataFrame, *, historical: bool = False, kind: str = "market") -> list[dict]:
    """依 df 實際含有的市場回傳 source 物件（上市、上櫃各一）。"""
    d = df["date"].iloc[0]
    auths = sorted({authority_for(t) for t in df["ticker"]}, reverse=True)  # twse 先
    return [source_info(d, a, historical=historical, kind=kind) for a in auths]


def update_latest(base: Path | None = None, fetcher: MarketFetcher | None = None) -> dict:
    """抓最新交易日行情並存檔，回傳 {date, path, missing, sources}。"""
    df = fetch_latest(fetcher)
    path = save_daily(df, base)[0]
    return {"date": df["date"].iloc[0], "path": path, "missing": check_coverage(df),
            "sources": sources_for_day(df)}


# --------------------------------------------------------------------------- 回補

def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def backfill(start: date, end: date, fetcher: MarketFetcher | None = None, base: Path | None = None,
             inst: bool = False, skip_existing: bool = True, log=print,
             inst_base: Path | None = None) -> list[str]:
    """回補 [start, end] 的歷史行情（可中斷續跑）。回傳寫出的交易日清單。

    上市：每檔每月打一次 STOCK_DAY（100 檔 × 月數），合併後得到交易日集合；
    上櫃：以該交易日集合逐日打 TPEx 日期查詢。inst=True 時同日再補三大法人。
    已存在 parquet 的交易日會略過（skip_existing）。
    """
    f = fetcher or MarketFetcher()
    uni = load_universe()
    twse_tickers = sorted(t for t, v in uni.items() if v["market"] == "TWSE")
    frames = []
    for y, m in _months(start, end):
        for t in twse_tickers:
            frames.append(f.twse_stock_month(t, y, m))
        log(f"TWSE {y}-{m:02d} done")
    twse = pd.concat([x for x in frames if not x.empty] or [pd.DataFrame(columns=MARKET_COLUMNS)],
                     ignore_index=True)
    twse = twse[(twse["date"] >= start.isoformat()) & (twse["date"] <= end.isoformat())]
    days = sorted(twse["date"].unique())
    written = []
    for ds in days:
        if skip_existing and market_path(ds, base).exists():
            continue
        d = date.fromisoformat(ds)
        day = pd.concat([twse[twse["date"] == ds], f.tpex_day(d)], ignore_index=True)
        save_daily(day, base)
        if inst:
            ip = institutional_path(ds, inst_base)
            if not (skip_existing and ip.exists()):
                save_daily(pd.concat([f.twse_inst(d), f.tpex_inst(d)], ignore_index=True), inst_base,
                           kind="institutional")
        written.append(ds)
        log(f"saved {ds} ({len(day)} rows, missing {len(check_coverage(day))})")
    return written
