"""美元兌新台幣：CNBC TWD= 為主、FRED DEXTAUS 為備援（DEXTAUS 每週才更新，實測 10/16 時最新只到 10/09）。

  CNBC  https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols=TWD%3D&...&output=json
        FormattedQuoteResult.FormattedQuote[{symbol: "TWD=", last: "32.429", change: "+0.087", change_pct, last_time}]
        （實測 2026-10-16：32.429，+0.087）。observation url 用 https://www.cnbc.com/quotes/TWD=，authority vendor。
新鮮度：日期早於預期最新美股交易日（us.expected_us_close(now)）就換下一個來源；全部都舊才用最新的並記警告。
輸出併入 macro.series["USDTWD"]（取代 DEXTAUS），chg 為日變動百分比（正值＝台幣貶值）。
"""
from __future__ import annotations

import json
from datetime import date, datetime

from .http import BROWSER_HEADERS, Fetcher, decode
from .us import CNBC_URL, _num, expected_us_close

CNBC_FX_URL = CNBC_URL.format(s="TWD%3D")
CNBC_FX_PAGE = "https://www.cnbc.com/quotes/TWD="
NAME = "美元兌新台幣匯率"


def parse_cnbc_fx(payload: dict) -> dict:
    """→ {"date", "value", "change", "chg_pct"}。change_pct 缺時以 change ÷ (last − change) 計算。"""
    quotes = (((payload or {}).get("FormattedQuoteResult") or {}).get("FormattedQuote")) or []
    q = next((x for x in quotes if x.get("symbol") == "TWD="), None)
    if q is None:
        raise ValueError("CNBC 回應沒有 TWD=：" + json.dumps(payload, ensure_ascii=False)[:120])
    last = _num(q["last"])
    d = date.fromisoformat(str(q["last_time"])[:10]).isoformat()
    change = _num(q["change"]) if str(q.get("change", "")).strip() not in ("", "UNCH") else 0.0
    try:
        pct = _num(q["change_pct"])
    except (KeyError, ValueError, TypeError):
        pct = change / (last - change) * 100
    return {"date": d, "value": last, "change": change, "chg_pct": pct}


def collect_fx(fetcher: Fetcher, now: datetime, macro: dict | None = None) -> tuple[dict | None, list[str]]:
    """回傳 (macro.series["USDTWD"] 項目或 None, warnings)。macro 為 collect_macro 的輸出（取其中的 DEXTAUS 當備援）。"""
    expected = expected_us_close(now).isoformat()
    cands, warns, errs = [], [], []
    try:
        raw = fetcher.get(CNBC_FX_URL, name="fx_cnbc_TWD.json", headers=BROWSER_HEADERS, retries=2)
        c = parse_cnbc_fx(json.loads(decode(raw)))
        cands.append({"name": NAME, "unit": " 台幣／美元", "date": c["date"], "value": c["value"], "chg": round(c["chg_pct"], 4),
                      "chg_unit": "%", "change": c["change"], "url": CNBC_FX_PAGE, "source": "CNBC TWD="})
    except Exception as e:  # noqa: BLE001 - 換備援
        errs.append(f"CNBC：{type(e).__name__}: {str(e).splitlines()[0][:80]}")
    fred = ((macro or {}).get("series") or {}).get("DEXTAUS")
    if fred:
        cands.append({**fred, "source": "FRED DEXTAUS"})
    else:
        errs.append("FRED DEXTAUS：缺值")
    for x in cands:
        if x["date"] >= expected:
            return x, warns
        errs.append(f"{x['source']}：最新 {x['date']} 早於預期 {expected}")
    if not cands:
        return None, [f"美元兌台幣全部來源失敗（{'；'.join(errs)}），缺值"]
    best = max(cands, key=lambda x: x["date"])
    return best, [f"美元兌台幣所有來源都早於預期 {expected}，採用最新的 {best['source']} {best['date']}（{'；'.join(errs)}）"]


def merge_fx(macro: dict | None, fx: dict | None) -> dict | None:
    """把匯率併進 macro.series["USDTWD"]，並移除 DEXTAUS（避免同一個匯率出現兩則）。"""
    if macro is None and fx is None:
        return None
    m = {"series": {}, "mode": "none"} if macro is None else {**macro, "series": dict(macro.get("series") or {})}
    m["series"].pop("DEXTAUS", None)
    if fx:
        m["series"]["USDTWD"] = fx
    return m if m["series"] else None
