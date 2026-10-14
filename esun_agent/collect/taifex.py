"""台指期夜盤：TAIFEX「期貨每日交易行情」下載（futDataDown，CSV，Big5）。

CSV 欄位：交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,
          最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量
夜盤列的「交易時段」為「盤後」；交易日期是夜盤所屬的（下一個）交易日，夜盤在該日 05:00 收盤。
取契約 TX、最近月份（排除週別 W 與價差 / 合約），回傳最新交易日期的夜盤。
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from .htmlrows import to_number
from .http import Fetcher, decode

TAIFEX_DOWN_URL = "https://www.taifex.com.tw/cht/3/futDataDown"
TAIFEX_PAGE = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"


def parse_fut_csv(text: str, contract: str = "TX") -> dict | None:
    """→ {session_date, contract_month, close, chg, chg_pct, volume}（最新一個夜盤）；沒有夜盤列時回傳 None。"""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return None
    hdr = [h.strip() for h in rows[0]]

    def col(name):
        return next(i for i, h in enumerate(hdr) if h.startswith(name))
    i_d, i_c, i_m, i_close = col("交易日期"), col("契約"), col("到期月份"), col("收盤價")
    i_chg, i_pct, i_vol, i_sess = col("漲跌價"), col("漲跌%"), col("成交量"), col("交易時段")
    best = {}
    for r in rows[1:]:
        if len(r) <= i_sess or r[i_c].strip() != contract or r[i_sess].strip() != "盤後":
            continue
        month = r[i_m].strip()
        if "W" in month.upper() or "/" in month:
            continue
        close = to_number(r[i_close])
        if close is None:
            continue
        d = r[i_d].strip().replace("/", "-")
        if d not in best or month < best[d]["contract_month"]:
            best[d] = {"session_date": d, "contract_month": month, "close": close, "chg": to_number(r[i_chg]),
                       "chg_pct": to_number(r[i_pct]), "volume": to_number(r[i_vol])}
    if not best:
        return None
    out = best[max(best)]
    if out["chg_pct"] is None and out["chg"] is not None:
        prev = out["close"] - out["chg"]
        out["chg_pct"] = out["chg"] / prev * 100 if prev else None
    return out


def taifex_input(n: dict) -> dict:
    """→ observations.taifex_observation 的輸入。content_as_of = 夜盤收盤（所屬交易日 05:00）。"""
    return {"url": TAIFEX_PAGE, "content_as_of": f"{n['session_date']}T05:00:00+08:00",
            "session_date": n["session_date"], "close": n["close"], "chg_pct": n["chg_pct"] or 0.0,
            **({"volume": n["volume"]} if n.get("volume") is not None else {}),
            "name": f"TAIFEX 台指期夜盤（{n['contract_month']}，所屬交易日 {n['session_date']}）"}


def collect_taifex(fetcher: Fetcher, start: date, end: date) -> tuple[dict | None, list[str]]:
    data = {"down_type": "1", "commodity_id": "TX", "queryStartDate": start.strftime("%Y/%m/%d"),
            "queryEndDate": (end + timedelta(days=1)).strftime("%Y/%m/%d")}
    try:
        n = parse_fut_csv(decode(fetcher.get(TAIFEX_DOWN_URL, data=data, name=f"taifex_TX_{end:%Y%m%d}.csv")))
    except Exception as e:  # noqa: BLE001
        return None, [f"台指期夜盤抓取失敗：{e}"]
    if n is None:
        return None, ["台指期夜盤：CSV 中沒有夜盤（盤後）資料"]
    return taifex_input(n), []
