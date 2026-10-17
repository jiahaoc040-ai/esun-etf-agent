"""FRED（聖路易聯準銀行）數列：正式 API 優先，沒有金鑰時退回 fredgraph.csv。

  正式 API  https://api.stlouisfed.org/fred/series/observations?series_id=…&api_key=…&file_type=json
            金鑰讀環境變數 FRED_API_KEY（daily_run 會先讀 .env）。
            即時抓取（daily_run 晚上／清晨）：不帶 realtime 參數，由 FRED 用它自己當天的版本。
            回測／歷史研究明確傳入 vintage 時才帶 realtime_start = realtime_end = vintage（ALFRED：取「vintage 當天看得到的
            版本」，避免事後修正值）；vintage 先夾到 FRED 的今天 min(vintage, 美國中部時間今天)——FRED 伺服器以
            America/Chicago 日期為準，台北日期常比它快一天，realtime 落在 FRED 的「未來」會 HTTP 400。
  退回      https://fred.stlouisfed.org/graph/fredgraph.csv?id=…（只有最新版本，無法指定 vintage；會回傳警告）。
  金鑰只放在 query 參數（params），不會出現在 source url、raw 檔名裡；錯誤訊息中的 api_key 一律遮蔽成 ***。
  HTTP 錯誤的警告包含狀態碼與 FRED 回傳的 error_message。source url 一律用數列頁 https://fred.stlouisfed.org/series/<id>。

總經數列（macro）：DGS2 2 年期公債殖利率、DFF 聯邦基金有效利率、VIXCLS VIX、T10Y2Y 10Y−2Y 利差、
DEXTAUS 美元兌新台幣（皆為日資料；DEXTAUS 寫日變動百分比）。
不用 DTWEXBGS（美元指數，每週更新）。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .http import BROWSER_HEADERS, Fetcher, decode

FRED_TZ = ZoneInfo("America/Chicago")                  # FRED 伺服器（聖路易聯準銀行）的日期基準
API_URL = "https://api.stlouisfed.org/fred/series/observations"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={s}"
SERIES_PAGE = "https://fred.stlouisfed.org/series/{s}"
TIMEOUT, RETRIES = 60, 3                               # 逾時 60 秒；第一次失敗後再重試 2 次
LOOKBACK_DAYS = 21                                     # 取近 3 週，足夠找到最後兩個有值的日期（跨假日）

# id → (中文名, 單位, 日變化的單位, 倍率)；倍率 None ＝ 日變動百分比 (v1 / v0 − 1) × 100
# DEXTAUS（美元兌新台幣，H.10）：數值上升＝台幣貶值；FRED 約每週一次補上前一週的日資料，觀測日常落後數日。
MACRO_SERIES = {
    "DGS2": ("美國 2 年期公債殖利率", "%", "bp", 100.0),
    "DFF": ("聯邦基金有效利率", "%", "bp", 100.0),
    "VIXCLS": ("VIX 波動率指數", "點", "點", 1.0),
    "T10Y2Y": ("美債 10 年減 2 年利差", "%", "bp", 100.0),
    "DEXTAUS": ("美元兌新台幣匯率", " 台幣／美元", "%", None),
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


def fred_today(now: datetime | None = None) -> date:
    """FRED 的「今天」：美國中部時間日期（台北 10/17 01:00 時仍是 10/16）。"""
    return (now or datetime.now(FRED_TZ)).astimezone(FRED_TZ).date()


def clamp_vintage(vintage: date, now: datetime | None = None) -> date:
    return min(vintage, fred_today(now))


def mask_key(text: str, key: str | None = None) -> str:
    """遮蔽 api_key：query 參數 api_key=… 與金鑰字串本身都換成 ***。"""
    out = re.sub(r"(api_key=)[^&\s'\"]+", r"\1***", str(text))
    key = key or api_key()
    return out.replace(key, "***") if key else out


class FredError(RuntimeError):
    pass


def _error_detail(exc: Exception) -> str:
    """從 Fetcher 的例外鏈找出 HTTP 狀態碼與 FRED 回傳的 error_message（JSON 或 XML）。"""
    e: BaseException | None = exc
    while e is not None:
        resp = getattr(e, "response", None)
        if resp is not None and getattr(resp, "status_code", None):
            msg = ""
            body = getattr(resp, "text", "") or ""
            try:
                msg = str(json.loads(body).get("error_message", ""))
            except (ValueError, AttributeError):
                m = re.search(r'error_message="([^"]*)"', body)
                msg = m.group(1) if m else body.strip()[:200]
            return f"HTTP {resp.status_code}" + (f"：{msg}" if msg else "")
        e = e.__cause__
    return f"{type(exc).__name__}: {exc}"


def fetch(fetcher: Fetcher, series_id: str, *, end: date | None = None, vintage: date | None = None,
          start: date | None = None, now: datetime | None = None) -> tuple[list[tuple[str, float]], str, str]:
    """回傳 (序列, source url（數列頁）, 方式 "api"|"csv")。有 FRED_API_KEY 用正式 API；沒有金鑰用 fredgraph.csv。
    vintage=None（即時）：不帶 realtime 參數；有 vintage（回測／歷史）：夾到 FRED 今天後帶 realtime_start/end。
    失敗丟 FredError（含 HTTP 狀態碼與 FRED error_message，金鑰遮蔽）。"""
    key = api_key()
    if key:
        end = end or fred_today(now)
        params = {"api_key": key, "file_type": "json", "sort_order": "asc",
                  "observation_start": (start or end - timedelta(days=LOOKBACK_DAYS)).isoformat(),
                  "observation_end": end.isoformat()}
        if vintage is not None:
            v = clamp_vintage(vintage, now).isoformat()
            params.update(realtime_start=v, realtime_end=v)
        try:
            raw = fetcher.get(API_URL + f"?series_id={series_id}", params=params,
                              name=f"fred_api_{series_id}.json", timeout=TIMEOUT, retries=RETRIES)
        except Exception as e:  # noqa: BLE001 - 轉成不含金鑰的訊息
            raise FredError(mask_key(f"FRED API {series_id} {_error_detail(e)}", key)) from None
        return parse_api(json.loads(decode(raw))), series_page(series_id), "api"
    url = CSV_URL.format(s=series_id)
    raw = fetcher.get(url, name=f"us_fred_{series_id}.csv", headers=BROWSER_HEADERS, timeout=TIMEOUT, retries=RETRIES)
    return parse_csv(decode(raw)), series_page(series_id), "csv"


def last_two(series: list[tuple[str, float]], not_after: date | None = None) -> tuple[tuple[str, float], tuple[str, float]]:
    s = [x for x in series if not_after is None or x[0] <= not_after.isoformat()]
    if len(s) < 2:
        raise ValueError("觀測值不足兩筆")
    return s[-2], s[-1]


def collect_macro(fetcher: Fetcher, not_after: date | None = None, vintage: date | None = None,
                  now: datetime | None = None) -> tuple[dict | None, list[str]]:
    """回傳 (macro_input 或 None, warnings)。
    macro_input = {"series": {id: {"name", "unit", "date", "value", "prev_date", "prev_value", "chg", "chg_unit", "url"}},
                   "mode": "api"|"csv"}。date 是觀測值的實際日期（不是今天）。
    vintage（ALFRED 版本日）：每日即時抓取不傳（不帶 realtime 參數）；回測／歷史研究才傳，會夾到 FRED 的今天。
    只有正式 API 支援 vintage，退回 CSV 時會警告。"""
    out, warns, modes = {}, [], set()
    for sid, (name, unit, chg_unit, mult) in MACRO_SERIES.items():
        try:
            series, url, mode = fetch(fetcher, sid, end=not_after, vintage=vintage, now=now)
            (d0, v0), (d1, v1) = last_two(series, not_after)
        except Exception as e:  # noqa: BLE001 - 單一數列失敗只缺值
            warns.append(f"FRED {sid} 失敗（{mask_key(str(e).splitlines()[0])[:200]}），缺值")
            continue
        modes.add(mode)
        out[sid] = {"name": name, "unit": unit, "date": d1, "value": v1, "prev_date": d0, "prev_value": v0,
                    "chg": round((v1 / v0 - 1) * 100 if mult is None else (v1 - v0) * mult, 4),
                    "chg_unit": chg_unit, "url": url}
    if not out:
        return None, warns + ["FRED 總經數列全部失敗，今日不使用 macro 資料"]
    if "csv" in modes:
        warns.append("未設定 FRED_API_KEY：改用 fredgraph.csv" + ("（無法指定 ALFRED 版本，歷史日期可能含事後修正值）" if vintage else ""))
    return {"series": out, "mode": "api" if modes == {"api"} else "csv"}, warns
