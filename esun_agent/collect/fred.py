"""FRED（聖路易聯準銀行）數列：正式 API 優先，沒有金鑰時退回 fredgraph.csv。

  正式 API  https://api.stlouisfed.org/fred/series/observations?series_id=…&api_key=…&file_type=json
            金鑰讀環境變數 FRED_API_KEY（daily_run 會先讀 .env）。一律帶 realtime_start = realtime_end = vintage
            （ALFRED：取「vintage 當天看得到的版本」），回測或補跑歷史日期時不會用到事後修正值。
  退回      https://fred.stlouisfed.org/graph/fredgraph.csv?id=…（只有最新版本，無法指定 vintage；會回傳警告）。
  金鑰只放在 query 參數（params），不會出現在 source url、警告或 raw 檔名裡；source url 一律用數列頁
  https://fred.stlouisfed.org/series/<id>。

總經數列（macro）：DGS2 2 年期公債殖利率、DFF 聯邦基金有效利率、VIXCLS VIX、T10Y2Y 10Y−2Y 利差（皆為日資料）。
不用 DTWEXBGS（美元指數，每週更新）。
"""
from __future__ import annotations

import csv
import io
import json
import os
from datetime import date, timedelta

from .http import BROWSER_HEADERS, Fetcher, decode

API_URL = "https://api.stlouisfed.org/fred/series/observations"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={s}"
SERIES_PAGE = "https://fred.stlouisfed.org/series/{s}"
TIMEOUT, RETRIES = 60, 3                               # 逾時 60 秒；第一次失敗後再重試 2 次
LOOKBACK_DAYS = 21                                     # 取近 3 週，足夠找到最後兩個有值的日期（跨假日）

# id → (中文名, 單位, 日變化的單位與倍率)
MACRO_SERIES = {
    "DGS2": ("美國 2 年期公債殖利率", "%", "bp", 100.0),
    "DFF": ("聯邦基金有效利率", "%", "bp", 100.0),
    "VIXCLS": ("VIX 波動率指數", "點", "點", 1.0),
    "T10Y2Y": ("美債 10 年減 2 年利差", "%", "bp", 100.0),
}


def api_key() -> str | None:
    return os.environ.get("FRED_API_KEY", "").strip() or None


def series_page(series_id: str) -> str:
    return SERIES_PAGE.format(s=series_id)


def parse_csv(text: str) -> list[tuple[str, float]]:
    rows = list(csv.reader(io.StringIO(text.strip().lstrip("﻿"))))
    if not rows or len(rows[0]) < 2 or "date" not in rows[0][0].lower():
        raise ValueError("FRED CSV 格式不符：" + text[:80])
    out = []
    for r in rows[1:]:
        if len(r) < 2 or r[1].strip() in ("", "."):
            continue
        try:
            out.append((r[0].strip(), float(r[1])))
        except ValueError:
            continue
    if not out:
        raise ValueError("FRED CSV 沒有數值")
    return out


def parse_api(payload: dict) -> list[tuple[str, float]]:
    """{"observations": [{"date", "value", "realtime_start", "realtime_end"}]}；value "." 為缺值（略過）。由舊到新。"""
    if "observations" not in (payload or {}):
        raise ValueError("FRED API 回應格式不符：" + json.dumps(payload, ensure_ascii=False)[:120])
    out = []
    for o in payload["observations"]:
        v = str(o.get("value", "")).strip()
        if v in ("", "."):
            continue
        try:
            out.append((str(o["date"]), float(v)))
        except (KeyError, ValueError):
            continue
    if not out:
        raise ValueError("FRED API 沒有數值")
    return sorted(out)


def fetch(fetcher: Fetcher, series_id: str, *, end: date | None = None, vintage: date | None = None,
          start: date | None = None) -> tuple[list[tuple[str, float]], str, str]:
    """回傳 (序列, source url（數列頁）, 方式 "api"|"csv")。有 FRED_API_KEY 用正式 API（帶 realtime_start/end = vintage，
    預設 vintage = end = 今天）；沒有金鑰用 fredgraph.csv。失敗丟例外（錯誤訊息不含金鑰）。"""
    key = api_key()
    if key:
        end = end or date.today()
        vintage = vintage or end
        params = {"series_id": series_id, "api_key": key, "file_type": "json", "sort_order": "asc",
                  "observation_start": (start or end - timedelta(days=LOOKBACK_DAYS)).isoformat(),
                  "observation_end": end.isoformat(),
                  "realtime_start": vintage.isoformat(), "realtime_end": vintage.isoformat()}
        raw = fetcher.get(API_URL + f"?series_id={series_id}", params={k: v for k, v in params.items() if k != "series_id"},
                          name=f"fred_api_{series_id}.json", timeout=TIMEOUT, retries=RETRIES)
        return parse_api(json.loads(decode(raw))), series_page(series_id), "api"
    url = CSV_URL.format(s=series_id)
    raw = fetcher.get(url, name=f"us_fred_{series_id}.csv", headers=BROWSER_HEADERS, timeout=TIMEOUT, retries=RETRIES)
    return parse_csv(decode(raw)), series_page(series_id), "csv"


def last_two(series: list[tuple[str, float]], not_after: date | None = None) -> tuple[tuple[str, float], tuple[str, float]]:
    s = [x for x in series if not_after is None or x[0] <= not_after.isoformat()]
    if len(s) < 2:
        raise ValueError("觀測值不足兩筆")
    return s[-2], s[-1]


def collect_macro(fetcher: Fetcher, not_after: date | None = None, vintage: date | None = None) -> tuple[dict | None, list[str]]:
    """回傳 (macro_input 或 None, warnings)。
    macro_input = {"series": {id: {"name", "unit", "date", "value", "prev_date", "prev_value", "chg", "chg_unit", "url"}},
                   "mode": "api"|"csv"}。date 是觀測值的實際日期（不是今天）。
    vintage（ALFRED 版本日）預設 = not_after；只有正式 API 支援，退回 CSV 時會警告。"""
    out, warns, modes = {}, [], set()
    for sid, (name, unit, chg_unit, mult) in MACRO_SERIES.items():
        try:
            series, url, mode = fetch(fetcher, sid, end=not_after, vintage=vintage or not_after)
            (d0, v0), (d1, v1) = last_two(series, not_after)
        except Exception as e:  # noqa: BLE001 - 單一數列失敗只缺值
            warns.append(f"FRED {sid} 失敗（{str(e).splitlines()[0][:80]}），缺值")
            continue
        modes.add(mode)
        out[sid] = {"name": name, "unit": unit, "date": d1, "value": v1, "prev_date": d0, "prev_value": v0,
                    "chg": round((v1 - v0) * mult, 4), "chg_unit": chg_unit, "url": url}
    if not out:
        return None, warns + ["FRED 總經數列全部失敗，今日不使用 macro 資料"]
    if "csv" in modes:
        warns.append("未設定 FRED_API_KEY：改用 fredgraph.csv（無法指定 ALFRED 版本，歷史日期可能含事後修正值）")
    return {"series": out, "mode": "api" if modes == {"api"} else "csv"}, warns
