"""HTML → 所有 <tr> 的儲存格文字（支援巢狀表格：每一層 tr 各自成列，內層儲存格不併入外層）。"""
from __future__ import annotations

import re
from html.parser import HTMLParser


class _Rows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._rows: list[list[str]] = []          # 開啟中的 tr（堆疊）
        self._cells: list[list[str] | None] = []  # 對應每個 tr 目前開啟中的儲存格

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._rows.append([])
            self._cells.append(None)
        elif tag in ("td", "th") and self._rows:
            self._cells[-1] = []
        elif tag == "br" and self._cells and self._cells[-1] is not None:
            self._cells[-1].append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._rows and self._cells[-1] is not None:
            self._rows[-1].append(re.sub(r"\s+", " ", "".join(self._cells[-1])).strip())
            self._cells[-1] = None
        elif tag == "tr" and self._rows:
            row = self._rows.pop()
            self._cells.pop()
            if row:
                self.rows.append(row)

    def handle_data(self, data):
        if self._cells and self._cells[-1] is not None:
            self._cells[-1].append(data)


def table_rows(html: str) -> list[list[str]]:
    p = _Rows()
    p.feed(html)
    p.close()
    return p.rows


def to_number(s) -> float | None:
    t = str(s).replace(",", "").replace("%", "").strip()
    if t in ("", "-", "--", "N/A", "不適用"):
        return None
    try:
        return float(t)
    except ValueError:
        return None
