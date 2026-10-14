"""共用 HTTP：重試、原始回應存檔（--save-raw）、best-effort 的 Wayback Machine 存檔（archive_url）。"""
from __future__ import annotations

import re
import time
from pathlib import Path

import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) esun-etf-agent/2"
WAYBACK_SAVE = "https://web.archive.org/save/"


class Fetcher:
    def __init__(self, raw_dir: Path | None = None, session: requests.Session | None = None, retries: int = 3,
                 timeout: float = 30, sleep: float = 0.0):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", UA)
        self.raw_dir, self.retries, self.timeout, self.sleep = raw_dir, retries, timeout, sleep

    def get(self, url: str, params: dict | None = None, *, data: dict | None = None, name: str | None = None) -> bytes:
        """GET（data 不為 None 時 POST）。成功時把原始內容存到 raw_dir/name（未給 name 則由 URL 推導）。"""
        last: Exception | None = None
        for attempt in range(self.retries):
            if self.sleep:
                time.sleep(self.sleep)
            try:
                if data is None:
                    r = self.session.get(url, params=params, timeout=self.timeout)
                else:
                    r = self.session.post(url, params=params, data=data, timeout=self.timeout)
                r.raise_for_status()
                self.save_raw(name or raw_name(url, params, data), r.content)
                return r.content
            except requests.RequestException as e:
                last = e
                if attempt + 1 < self.retries:
                    time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"抓取失敗 {url}: {last}") from last

    def save_raw(self, name: str, content: bytes) -> Path | None:
        if self.raw_dir is None:
            return None
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        p = self.raw_dir / name
        p.write_bytes(content)
        return p


def raw_name(url: str, params: dict | None = None, data: dict | None = None) -> str:
    base = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "index"
    extra = [str(v) for v in list((params or {}).values()) + list((data or {}).values()) if str(v)]
    return re.sub(r"[^0-9A-Za-z_.\-]", "-", "_".join([base] + extra))[:150]


def decode(content: bytes, encodings=("utf-8", "cp950", "big5")) -> str:
    """MOPS／TAIFEX 舊頁面常是 Big5（cp950）。依序嘗試；全失敗時以 utf-8 replace 解。"""
    for enc in encodings:
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def archive(url: str, session: requests.Session | None = None, timeout: float = 25) -> str | None:
    """請 web.archive.org 存檔，回傳存檔網址；任何失敗都回傳 None（不影響流程）。"""
    s = session or requests.Session()
    try:
        r = s.get(WAYBACK_SAVE + url, timeout=timeout, headers={"User-Agent": UA}, allow_redirects=False)
        loc = r.headers.get("Content-Location") or r.headers.get("Location") or ""
        if loc.startswith("/web/"):
            return "https://web.archive.org" + loc
        if loc.startswith("https://web.archive.org/web/"):
            return loc
        m = re.search(r"/web/\d{14}/\S+", r.text[:2000] if r.ok else "")
        return "https://web.archive.org" + m.group(0) if m else None
    except requests.RequestException:
        return None
