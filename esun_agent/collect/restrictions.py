"""交易限制：處置股票、注意股票、暫停交易（停牌），上市＋上櫃，只保留 universe_150。

  上市  處置 https://openapi.twse.com.tw/v1/announcement/punish
        注意 https://openapi.twse.com.tw/v1/announcement/notice
        停牌 https://openapi.twse.com.tw/v1/exchangeReport/TWTAWU
  上櫃  處置 https://www.tpex.org.tw/openapi/v1/tpex_disposal_information
        注意 https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information

規則（交易日 T；程式強制，見 conviction_agent.apply_restrictions）：
  處置（disposal）：不得 BUY／ADD（已持有可續抱或賣出）。
  注意（attention）：不新建倉（未持有者不買；已持有者照常）。
  停牌（suspended）：不得出現在 orders；已持有者視為鎖定部位（目標＝現權重，計入檔數與權重），以 no_trade_decision 說明。

OpenAPI 欄位名稱各端點不同且未在雲端驗證（首次在 Mac 執行請核對 raw 檔），所以 parser 以關鍵字找欄位：
  代號：鍵名含 code（Code、SecuritiesCompanyCode…）或「代號」，值為 4–6 碼。
  期間：鍵名含 period／「期間」，值形如「115/10/20～115/11/02」或「1151020-1151102」→ 只保留涵蓋 T 者；
        停牌另找「恢復」／resum 欄位，恢復日 ≤ T 者不算停牌。
  沒有期間欄位 → 視為當期有效（注意股為 D 日公告、適用 T 日）。
任一端點失敗只記警告（該類別缺值），不影響其他端點。
"""
from __future__ import annotations

import json
import re
from datetime import date

from ..data.market import parse_roc_date
from ..universe import load_universe
from .http import Fetcher, decode

ENDPOINTS = {
    ("twse", "disposal"): "https://openapi.twse.com.tw/v1/announcement/punish",
    ("twse", "attention"): "https://openapi.twse.com.tw/v1/announcement/notice",
    ("twse", "suspended"): "https://openapi.twse.com.tw/v1/exchangeReport/TWTAWU",
    ("tpex", "disposal"): "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information",
    ("tpex", "attention"): "https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information",
}
KINDS = ("disposal", "attention", "suspended")
KIND_LABELS = {"disposal": "處置", "attention": "注意", "suspended": "暫停交易"}
DATE_RE = re.compile(r"(\d{2,4})[/\-.](\d{1,2})[/\-.](\d{1,2})|(?<!\d)(\d{7,8})(?!\d)")


def _code(r: dict) -> str | None:
    for k, v in r.items():
        kl = k.lower()
        if ("code" in kl or "代號" in k) and re.fullmatch(r"\d{4,6}[A-Z]?", str(v).strip()):
            return str(v).strip()
    return None


def _dates(text: str) -> list[date]:
    out = []
    for m in DATE_RE.finditer(str(text)):
        y, mo, d, compact = m.groups()
        s = compact or (f"{int(y):03d}{int(mo):02d}{int(d):02d}" if len(y) <= 3 else f"{y}{int(mo):02d}{int(d):02d}")
        try:
            out.append(parse_roc_date(s))
        except ValueError:
            continue
    return out


def _period(r: dict) -> tuple[date, date] | None:
    for k, v in r.items():
        if "period" in k.lower() or "期間" in k:
            ds = _dates(v)
            if len(ds) >= 2:
                return ds[0], ds[1]
            if len(ds) == 1:
                return ds[0], ds[0]
    return None


def _resume(r: dict) -> date | None:
    for k, v in r.items():
        if "resum" in k.lower() or "恢復" in k:
            ds = _dates(v)
            if ds:
                return ds[0]
    return None


def parse_records(records, kind: str, T: date, universe: dict | None = None) -> dict[str, dict]:
    """OpenAPI 陣列 → {ticker: {"period": [start, end] 或缺, "detail": 原始列的短摘要}}（只保留 universe 且 T 有效者）。"""
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
        per = _period(r)
        if per and not (per[0] <= T <= per[1]):
            continue
        if kind == "suspended":
            res = _resume(r)
            if res and res <= T:
                continue
        info = {"detail": "；".join(f"{k}={str(v).strip()}" for k, v in r.items() if str(v).strip())[:200]}
        if per:
            info["period"] = [per[0].isoformat(), per[1].isoformat()]
        out[t] = info
    return out


def collect_restrictions(fetcher: Fetcher, T: date, universe: dict | None = None) -> tuple[dict, list[str]]:
    """回傳 ({"trade_date", "disposal", "attention", "suspended", "sources": [{authority, url, kind}]}, warnings)。"""
    res: dict = {"trade_date": T.isoformat(), **{k: {} for k in KINDS}, "sources": []}
    warns: list[str] = []
    for (auth, kind), url in ENDPOINTS.items():
        try:
            raw = fetcher.get(url, name=f"restrict_{auth}_{kind}.json", retries=2)
            got = parse_records(json.loads(decode(raw)), kind, T, universe)
        except Exception as e:  # noqa: BLE001 - 單一端點失敗只缺值
            warns.append(f"{'上市' if auth == 'twse' else '上櫃'}{KIND_LABELS[kind]}股票抓取失敗"
                         f"（{type(e).__name__}: {str(e).splitlines()[0][:80]}），該類缺值")
            continue
        for t, info in got.items():
            res[kind][t] = {**info, "authority": auth}
        res["sources"].append({"authority": auth, "url": url, "kind": kind})
    return res, warns
