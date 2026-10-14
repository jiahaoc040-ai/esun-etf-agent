"""人工提供資料的收件匣：data/inbox/<交易日 YYYY-MM-DD>/ → 事件（observations 的 events 輸入格式）。

放法：每份文件（.md／.txt／.pdf／.html／.htm）旁邊放一個同名的 .meta.json
（`報告.pdf` 配 `報告.meta.json`，或 `報告.pdf.meta.json`），內容：
  {"url": 必填, "authority": 必填（media／fininst／mops／twse／tpex／taifex／vendor／other）,
   "published_at": 必填（ISO 8601，例如 2026-10-26T15:30:00+08:00）, "title": 必填,
   "archive_url": 選填, "ticker": 選填（150 檔之一）, "basis": 選填（五種依據 id，須符合宣告檔的 authority）,
   "values": 選填（{名稱: 數字}）}

規則（避免人為介入決策）：
  - 缺 meta、meta 缺 url／authority／published_at／title、authority 不合法、時間格式錯誤、檔案讀不出文字 → 略過並警告。
  - 只接受事實性內容：文字含「買進／賣出／加碼／減碼／建議」等指示性語句，且來源不是金融機構報告（authority ≠ fininst）→ 略過並警告。
  - published_at 晚於決策日 D 19:30（繳交起點）→ 略過並警告（這類內容 Agent 不可引用）。
  - 收件匣所有檔案（含被略過者）都複製到 runs/<交易日>/raw/inbox/ 留存，供 12/2 審查。
observation 只寫事實：statement = 標題＋開頭摘錄（≤ 500 字）；values = meta 提供的數字，否則為文件字數與提及的 150 檔檔數。
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from ..config import ROOT
from ..universe import load_universe

INBOX_DIR = ROOT / "data" / "inbox"
RUNS_DIR = ROOT / "runs"
DOC_SUFFIXES = {".md", ".txt", ".pdf", ".html", ".htm"}
AUTHORITIES = {"twse", "tpex", "taifex", "mops", "fininst", "media", "vendor", "other"}
REQUIRED_META = ("url", "authority", "published_at", "title")
DIRECTIVE_RE = re.compile(r"買進|賣出|加碼|減碼|建議|買入|目標價")
DIRECTIVE_OK_AUTHORITY = "fininst"
EVIDENCE_CUTOFF_TIME = "19:30:00+08:00"
STATEMENT_MAX = 500


@dataclass
class InboxResult:
    events: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)       # 被採用的檔名
    skipped: list[str] = field(default_factory=list)        # 被略過的檔名
    copied_to: Path | None = None


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def extract_text(path: Path) -> str:
    """文件 → 純文字（空白壓成單一空格）。PDF 用 pypdf。讀不出來丟 ValueError。"""
    suf = path.suffix.lower()
    if suf in (".md", ".txt"):
        text = path.read_text(encoding="utf-8", errors="replace")
    elif suf in (".html", ".htm"):
        p = _Text()
        p.feed(path.read_text(encoding="utf-8", errors="replace"))
        text = " ".join(p.parts)
    elif suf == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:                                    # pragma: no cover - requirements 已列 pypdf
            raise ValueError("需要 pypdf 才能讀 PDF") from e
        try:
            text = " ".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
        except Exception as e:  # noqa: BLE001 - 壞檔一律略過
            raise ValueError(f"PDF 無法解析：{type(e).__name__}") from e
    else:
        raise ValueError(f"不支援的格式 {suf}")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise ValueError("讀不出文字")
    return text


def find_meta(doc: Path) -> Path | None:
    for cand in (doc.with_suffix(".meta.json"), doc.with_name(doc.name + ".meta.json")):
        if cand.exists():
            return cand
    return None


def _parse_time(s: str) -> datetime:
    dt = datetime.fromisoformat(str(s))
    if dt.tzinfo is None:
        raise ValueError("published_at 需含時區（+08:00）")
    return dt


def mentioned_tickers(text: str) -> list[str]:
    uni = load_universe()
    found = []
    for t in dict.fromkeys(re.findall(r"(?<!\d)(\d{4})(?!\d)", text)):
        if t in uni:
            found.append(t)
    for t, info in uni.items():
        if t not in found and info["name"] and len(info["name"]) >= 2 and info["name"] in text:
            found.append(t)
    return found


def _statement(title: str, text: str) -> str:
    head = f"{title}：" if title else ""
    body = text if not text.startswith(title) else text[len(title):].lstrip(" ：:")
    st = head + body
    return st if len(st) <= STATEMENT_MAX else st[:STATEMENT_MAX - 1] + "…"


def load_inbox(trade_date: str, prev_date: str | None = None, *, base: Path | None = None,
               runs_dir: Path | None = None, copy_raw: bool = True) -> InboxResult:
    """讀 data/inbox/<trade_date>/。prev_date = 決策日 D（判斷 published_at 是否晚於 D 19:30；None 時不檢查）。"""
    src = (base or INBOX_DIR) / trade_date
    res = InboxResult()
    if not src.is_dir():
        return res
    if copy_raw:
        dst = (runs_dir or RUNS_DIR) / trade_date / "raw" / "inbox"
        dst.mkdir(parents=True, exist_ok=True)
        for f in sorted(src.iterdir()):
            if f.is_file():
                shutil.copy2(f, dst / f.name)
        res.copied_to = dst
    cutoff = _parse_time(f"{prev_date}T{EVIDENCE_CUTOFF_TIME}") if prev_date else None
    uni = load_universe()
    metas = {p for p in src.iterdir() if p.name.endswith(".meta.json")}
    for doc in sorted(p for p in src.iterdir() if p.is_file() and p not in metas):
        name = doc.name

        def skip(msg: str) -> None:
            res.warnings.append(f"收件匣 {name}：{msg}，略過")
            res.skipped.append(name)

        if doc.suffix.lower() not in DOC_SUFFIXES:
            skip(f"不支援的格式 {doc.suffix}（只收 {'、'.join(sorted(DOC_SUFFIXES))}）")
            continue
        mp = find_meta(doc)
        if mp is None:
            skip("缺少同名 .meta.json")
            continue
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            skip(f"{mp.name} 不是合法 JSON")
            continue
        if not isinstance(meta, dict):
            skip(f"{mp.name} 必須是 JSON 物件")
            continue
        missing = [k for k in REQUIRED_META if not str(meta.get(k) or "").strip()]
        if missing:
            skip(f"meta 缺少 {'、'.join(missing)}")
            continue
        url = str(meta["url"]).strip()
        if not re.match(r"^https?://", url):
            skip(f"url 不是 http(s) 網址：{url!r}")
            continue
        auth = str(meta["authority"]).strip()
        if auth not in AUTHORITIES:
            skip(f"authority {auth!r} 不合法（{'／'.join(sorted(AUTHORITIES))}）")
            continue
        try:
            pub = _parse_time(meta["published_at"])
        except (ValueError, TypeError) as e:
            skip(f"published_at 格式錯誤（{e}）")
            continue
        if cutoff is not None and pub > cutoff:
            skip(f"published_at {pub.isoformat()} 晚於決策日 {prev_date} 19:30，Agent 不可引用")
            continue
        try:
            text = extract_text(doc)
        except ValueError as e:
            skip(str(e))
            continue
        hits = sorted(set(DIRECTIVE_RE.findall(text + " " + str(meta["title"]))))
        if hits and auth != DIRECTIVE_OK_AUTHORITY:
            skip(f"含指示性語句（{'、'.join(hits)}）且來源不是金融機構報告（authority={auth}），只接受事實性內容")
            continue
        ticker = meta.get("ticker")
        if ticker is not None and str(ticker) not in uni:
            skip(f"ticker {ticker!r} 不在 150 檔名單")
            continue
        basis = meta.get("basis")
        if basis is not None:
            from ..strategy.declaration import allowed_bases, load_declaration
            allowed = {b["id"]: b["authority"] for b in allowed_bases(load_declaration())}
            if basis not in allowed:
                skip(f"basis {basis!r} 不是五種依據之一（{'／'.join(sorted(allowed))}）")
                continue
            if auth not in allowed[basis]:
                skip(f"basis={basis} 的來源 authority 須為 {'／'.join(allowed[basis])}，收到 {auth}")
                continue
            if not ticker:
                skip("指定 basis 時必須同時指定 ticker")
                continue
        tickers = mentioned_tickers(text)
        values = meta.get("values") or {"doc_chars": len(text), "tickers_mentioned": len(tickers)}
        if not isinstance(values, dict) or not values or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in values.values()):
            skip("meta.values 只能放數字")
            continue
        published = pub.isoformat()
        ev = {"basis": basis, "ticker": str(ticker) if ticker else None, "authority": auth, "url": url,
              "name": str(meta["title"])[:300], "published_at": published, "content_as_of": published,
              "statement": _statement(str(meta["title"]).strip(), text), "values": values,
              "inbox_file": name, "tickers_mentioned": tickers}
        if meta.get("archive_url"):
            ev["archive_url"] = str(meta["archive_url"])
        res.events.append(ev)
        res.accepted.append(name)
    return res


def merge_into_inputs(inputs: dict | None, res: InboxResult) -> dict:
    """把收件匣事件加進 DayContext.inputs["events"]（不改動原 dict）。"""
    out = dict(inputs or {})
    out["events"] = list(out.get("events", [])) + list(res.events)
    return out
