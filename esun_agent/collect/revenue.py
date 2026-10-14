"""MOPS 月營收（上市 sii、上櫃 otc）：彙總表 t21sc03_<民國年>_<月>_0.html。

表格每列：公司代號｜公司名稱｜當月營收｜上月營收｜去年當月營收｜上月比較增減(%)｜去年同月增減(%)｜
          當月累計營收｜去年累計營收｜前期比較增減(%)｜備註（金額單位：千元）。
YoY／MoM 由金額自行計算（不依賴表上的百分比欄，避免四捨五入與 N/A）。
「創新高」：與本機歷史（data/revenue/<YYYY-MM>.parquet）比較；歷史少於 min_history 個月時不判斷（is_new_high 缺值）。
各公司公告日不在彙總表上：first_seen 記錄本機第一次看到的時間；observation 的 content_as_of 用營收所屬月份月底
（撰寫指南：content_as_of 是內容「描述的時點」），published_at 用 first_seen。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from ..config import ROOT
from ..universe import load_universe
from .htmlrows import table_rows, to_number
from .http import Fetcher, decode

MOPS_REVENUE_BASE = "https://mopsov.twse.com.tw/nas/t21"
REVENUE_DIR = ROOT / "data" / "revenue"
COLUMNS = ["ym", "market", "ticker", "name", "revenue", "prev_month", "last_year", "cum", "cum_last_year", "note",
           "first_seen"]


def revenue_url(market: str, year: int, month: int) -> str:
    if market not in ("sii", "otc"):
        raise ValueError(market)
    return f"{MOPS_REVENUE_BASE}/{market}/t21sc03_{year - 1911}_{month}_0.html"


def parse_t21sc03(html: str, market: str, year: int, month: int, universe: dict | None = None) -> pd.DataFrame:
    """彙總表 → DataFrame（只留 150 檔；金額單位千元）。找不到任何公司列時丟 ValueError（格式改版）。"""
    uni = universe if universe is not None else load_universe()
    rows = []
    seen_any = False
    for r in table_rows(html):
        if len(r) < 10 or not r[0].strip().isdigit() or len(r[0].strip()) != 4:
            continue
        seen_any = True
        t = r[0].strip()
        if t not in uni:
            continue
        rev, prev, ly = to_number(r[2]), to_number(r[3]), to_number(r[4])
        if rev is None:
            continue
        rows.append({"ym": f"{year:04d}-{month:02d}", "market": market, "ticker": t, "name": r[1].strip(),
                     "revenue": rev, "prev_month": prev, "last_year": ly, "cum": to_number(r[7]),
                     "cum_last_year": to_number(r[8]), "note": (r[10].strip() if len(r) > 10 else "")})
    if not seen_any:
        raise ValueError(f"t21sc03 {market} {year}-{month:02d}：找不到任何公司列（格式可能改版）")
    return pd.DataFrame(rows, columns=COLUMNS[:-1])


def month_end(ym: str) -> date:
    y, m = int(ym[:4]), int(ym[5:])
    return date(y, m, calendar.monthrange(y, m)[1])


def last_reported_month(today: date) -> tuple[int, int]:
    """今天可能正在公布的營收月份：上個月。"""
    return (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)


# --------------------------------------------------------------------------- 本機歷史

def path_for(ym: str, base: Path | None = None) -> Path:
    return (base or REVENUE_DIR) / f"{ym}.parquet"


def merge_store(df: pd.DataFrame, fetched_at: datetime, base: Path | None = None) -> pd.DataFrame:
    """寫入（或更新）該月份檔；保留既有列的 first_seen，新出現的公司 first_seen = fetched_at。回傳合併後的整月資料。"""
    if df.empty:
        return df
    ym = df["ym"].iloc[0]
    p = path_for(ym, base)
    new = df.copy()
    new["first_seen"] = fetched_at.isoformat()
    if p.exists():
        old = pd.read_parquet(p)
        keep = old.set_index(["market", "ticker"])["first_seen"]
        new["first_seen"] = [keep.get((m, t), fetched_at.isoformat()) for m, t in zip(new["market"], new["ticker"])]
        old = old[~old.set_index(["market", "ticker"]).index.isin(new.set_index(["market", "ticker"]).index)]
        new = pd.concat([old, new], ignore_index=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    new[COLUMNS].sort_values(["market", "ticker"]).reset_index(drop=True).to_parquet(p, index=False)
    return new


def load_history(base: Path | None = None) -> pd.DataFrame:
    files = sorted((base or REVENUE_DIR).glob("????-??.parquet"))
    if not files:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)


@dataclass
class RevenueFact:
    ticker: str
    ym: str
    revenue: float          # 千元
    mom_pct: float | None
    yoy_pct: float | None
    cum_yoy_pct: float | None
    is_new_high: bool | None
    history_months: int
    market: str
    first_seen: str


def facts(month_df: pd.DataFrame, history: pd.DataFrame, min_history: int = 24) -> dict[str, RevenueFact]:
    """month_df：單月資料（含 first_seen）。history：所有月份（可含本月）。"""
    out = {}
    for _, r in month_df.iterrows():
        h = history[(history["ticker"] == r["ticker"]) & (history["ym"] < r["ym"])]
        n = int(h["ym"].nunique())
        high = None if n < min_history else bool(r["revenue"] > h["revenue"].max())

        def pct(a, b):
            return None if a is None or b is None or pd.isna(a) or pd.isna(b) or not b else (a / b - 1) * 100
        out[r["ticker"]] = RevenueFact(r["ticker"], r["ym"], float(r["revenue"]), pct(r["revenue"], r["prev_month"]),
                                       pct(r["revenue"], r["last_year"]), pct(r["cum"], r["cum_last_year"]), high, n,
                                       r["market"], str(r["first_seen"]))
    return out


def fact_event(f: RevenueFact, url: str, archive_url: str | None = None) -> dict:
    """月營收事實 → observations.event_observation 的輸入（basis=monthly_revenue、authority=mops）。"""
    y, m = int(f.ym[:4]), int(f.ym[5:])
    parts = [f"{f.ticker} {y} 年 {m} 月營收 {f.revenue / 1e5:,.2f} 億元"]
    values = {"revenue_twd_100m": round(f.revenue / 1e5, 4)}
    if f.yoy_pct is not None:
        parts.append(f"年增 {f.yoy_pct:+.1f}%")
        values["yoy_pct"] = round(f.yoy_pct, 2)
    if f.mom_pct is not None:
        parts.append(f"月增 {f.mom_pct:+.1f}%")
        values["mom_pct"] = round(f.mom_pct, 2)
    if f.cum_yoy_pct is not None:
        parts.append(f"累計年增 {f.cum_yoy_pct:+.1f}%")
        values["cum_yoy_pct"] = round(f.cum_yoy_pct, 2)
    if f.is_new_high is not None:
        parts.append(f"{'創' if f.is_new_high else '未創'}近 {f.history_months} 個月新高")
        values["is_new_high"] = int(f.is_new_high)
        values["history_months"] = f.history_months
    ev = {"basis": "monthly_revenue", "ticker": f.ticker, "authority": "mops", "url": url,
          "name": f"MOPS 月營收彙總表（{'上市' if f.market == 'sii' else '上櫃'} {y}-{m:02d}）",
          "content_as_of": f"{month_end(f.ym).isoformat()}T23:59:59+08:00", "published_at": f.first_seen,
          "statement": "，".join(parts), "values": values}
    if archive_url:
        ev["archive_url"] = archive_url
    return ev


def collect_revenue(fetcher: Fetcher, year: int, month: int, *, fetched_at: datetime, base: Path | None = None,
                    min_history: int = 24) -> tuple[dict[str, RevenueFact], dict[str, str], list[str]]:
    """抓上市、上櫃當月彙總表並寫入本機歷史。回傳 (facts, {market: url}, warnings)。單一市場失敗只記警告。"""
    warns, urls, months = [], {}, []
    for mk in ("sii", "otc"):
        url = revenue_url(mk, year, month)
        try:
            html = decode(fetcher.get(url, name=f"mops_t21sc03_{mk}_{year}{month:02d}.html"))
            df = parse_t21sc03(html, mk, year, month)
        except Exception as e:  # noqa: BLE001 - 單一來源失敗不中斷
            warns.append(f"月營收 {mk} {year}-{month:02d} 抓取失敗：{e}")
            continue
        urls[mk] = url
        merged = merge_store(df, fetched_at, base)
        months.append(merged[merged["market"] == mk])
    if not months:
        return {}, urls, warns
    hist = load_history(base)
    return facts(pd.concat(months, ignore_index=True), hist, min_history), urls, warns
