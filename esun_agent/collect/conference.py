"""法人說明會（公開資訊觀測站 t100sb02_1「法人說明會一覽表」），150 檔，寫入 config/event_calendar.json 的 stock_events。

  POST https://mopsov.twse.com.tw/mops/web/ajax_t100sb02_1
       encodeURIComponent=1&step=1&firstin=1&off=1&TYPEK=<sii|otc>&year=<民國年>&month=<月>
  回應 HTML 表格：公司代號｜公司名稱｜召開法人說明會日期（115/10/28 或 115/10/28 至 115/10/29）｜時間｜地點｜擇要訊息｜…
端點與欄位未在雲端驗證（fixture 為推測格式）；首次在 Mac 執行請核對（scripts/fetch_conferences.py --save-raw）。

分類：只有「公司自辦的季度／年度財報法說」標 kind=investor_conference（摘要含「第三季」「第3季」「Q3」「營運成果」
「財務」「營運報告」「展望」其一，且不含「受邀」「邀請」「參加」）；其餘（券商邀約的投資人會議等）標 kind=broker_meeting，
仍寫進 event_calendar 但不觸發任何限制。同一檔同一天只留一筆（兩種都有時留 investor_conference）。
實測：2317 鴻海 10/20「受凱基證券邀請參加投資人會議」→ broker_meeting；2303 聯電 10/28「2026年第三季財務暨營運報告」
→ investor_conference。

規則（conviction_agent）：investor_conference 前 1 個交易日不新建倉；法說後重大訊息含財測／展望上修字樣 → 提示 Agent 可據此提高信心一級
（必須引用該重大訊息 observation）。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..universe import load_universe
from .htmlrows import table_rows
from .http import Fetcher, decode
from .restrictions import STOCK_CODE_RE, _dates

MOPS_CONF_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t100sb02_1"
MOPS_CONF_PAGE = "https://mopsov.twse.com.tw/mops/web/t100sb02_1"
KIND = "investor_conference"
BROKER_KIND = "broker_meeting"
EARNINGS_WORDS = ("第三季", "第3季", "Q3", "營運成果", "財務", "營運報告", "展望")
INVITED_WORDS = ("受邀", "邀請", "參加")
UPGRADE_WORDS = ("上修", "調升", "調高", "優於預期", "財測上調", "展望樂觀", "上調")
UPGRADE_CONTEXT = ("財測", "展望", "營運", "預估", "指引", "目標")


def classify(summary: str) -> str:
    """公司自辦的季度／年度財報法說 → investor_conference；券商邀約等 → broker_meeting。"""
    s = str(summary)
    if any(w in s for w in EARNINGS_WORDS) and not any(w in s for w in INVITED_WORDS):
        return KIND
    return BROKER_KIND


def parse_t100sb02(html: str, universe: dict | None = None) -> list[dict]:
    """→ [{"ticker", "name", "date", "summary", "kind"}]（只留 universe 內 4 碼普通股；同一檔同日只留一筆，
    兩種都有時留 investor_conference）。"""
    uni = universe if universe is not None else load_universe()
    rows = table_rows(html)
    head = next((i for i, r in enumerate(rows) if any("公司代號" in c for c in r) and any("說明會日期" in c for c in r)), None)
    if head is None:
        raise ValueError("法人說明會表格找不到「公司代號」與「說明會日期」欄（格式改版或查無資料）")
    h = rows[head]
    ic = next(i for i, c in enumerate(h) if "公司代號" in c)
    iname = next((i for i, c in enumerate(h) if "公司名稱" in c), None)
    idate = next(i for i, c in enumerate(h) if "說明會日期" in c)
    isum = next((i for i, c in enumerate(h) if "擇要" in c), None)
    out, seen = [], {}
    for r in rows[head + 1:]:
        if len(r) <= max(ic, idate):
            continue
        t = r[ic].strip()
        if not STOCK_CODE_RE.fullmatch(t) or t not in uni:
            continue
        ds = _dates(r[idate])
        if not ds:
            continue
        summary = (r[isum] if isum is not None and isum < len(r) else "").strip()
        row = {"ticker": t, "name": (r[iname] if iname is not None and iname < len(r) else uni[t]["name"]).strip(),
               "date": ds[0].isoformat(), "summary": summary[:200], "kind": classify(summary)}
        key = (t, row["date"])
        if key in seen:
            if row["kind"] == KIND and seen[key]["kind"] != KIND:
                seen[key].update(row)
            continue
        seen[key] = row
        out.append(row)
    return out


def months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def collect_conferences(fetcher: Fetcher, start: date, end: date, universe: dict | None = None
                        ) -> tuple[list[dict], list[str]]:
    """[start, end] 期間 150 檔的法說會 → stock_events 格式 [{date, kind, ticker, name, url}]。"""
    events, warns, seen = [], [], {}
    for y, m in months(start, end):
        for typek in ("sii", "otc"):
            data = {"encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1", "TYPEK": typek,
                    "year": str(y - 1911), "month": f"{m:02d}"}
            try:
                html = decode(fetcher.get(MOPS_CONF_URL, data=data, name=f"t100sb02_{typek}_{y}{m:02d}.html"))  # POST
                rows = parse_t100sb02(html, universe)
            except Exception as e:  # noqa: BLE001
                warns.append(f"法人說明會 {typek} {y}-{m:02d} 抓取失敗（{type(e).__name__}: {str(e).splitlines()[0][:80]}）")
                continue
            for r in rows:
                if not start.isoformat() <= r["date"] <= end.isoformat():
                    continue
                key = (r["ticker"], r["date"])
                e = {"date": r["date"], "kind": r["kind"], "ticker": r["ticker"],
                     "name": f"{r['ticker']} {r['name']} " + ("法人說明會" if r["kind"] == KIND else "投資人會議"),
                     "url": MOPS_CONF_PAGE, "summary": r["summary"]}
                if key in seen:
                    if e["kind"] == KIND and seen[key]["kind"] != KIND:
                        seen[key].update(e)
                    continue
                seen[key] = e
                events.append(e)
    events.sort(key=lambda e: (e["date"], e["ticker"]))
    return events, warns


def write_stock_events(path: Path, events: list[dict], start: date, end: date) -> dict:
    """更新 event_calendar.json 的 stock_events：[start, end] 內的法說會／投資人會議以新抓到的取代，其餘保留。"""
    cal = json.loads(path.read_text(encoding="utf-8"))
    keep = [e for e in cal.get("stock_events", [])
            if not (e.get("kind") in (KIND, BROKER_KIND) and start.isoformat() <= e["date"] <= end.isoformat())]
    cal["stock_events"] = sorted(keep + events, key=lambda e: (e["date"], e["ticker"]))
    path.write_text(json.dumps(cal, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return cal


def is_upgrade_news(statement: str) -> bool:
    """重大訊息是否含財測／展望上修字樣（例如「上修全年營收展望」「調升財測」）。"""
    s = str(statement)
    return any(w in s for w in UPGRADE_WORDS) and any(c in s for c in UPGRADE_CONTEXT)
