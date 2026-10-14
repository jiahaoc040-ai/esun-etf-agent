# 人工收件匣

把新聞或報告放進 `data/inbox/<交易日 YYYY-MM-DD>/`（交易日 = 要繳交的 D-Plan 日期，例如 10/26 晚上放進 `2026-10-27/`）。

每份文件（`.md`／`.txt`／`.pdf`／`.html`）旁邊放一個同名的 `.meta.json`，例如 `tsmc.md` 配 `tsmc.meta.json`（或 `tsmc.md.meta.json`）：

```json
{"url": "https://news.cnyes.com/news/id/123", "authority": "media",
 "published_at": "2026-10-26T15:30:00+08:00", "title": "台積電九月營收創新高",
 "archive_url": "https://web.archive.org/...", "ticker": "2330", "basis": null, "values": {"rev_twd_100m": 3200}}
```

- 必填：`url`（http/https）、`authority`（media／fininst／mops／twse／tpex／taifex／vendor／other）、`published_at`（含 +08:00）、`title`。
- 選填：`archive_url`、`ticker`（150 檔之一）、`basis`（五種依據 id；authority 必須符合宣告檔，例如月營收須為 mops，且要同時填 ticker）、`values`（只放數字）。
- **只接受事實性內容**：含「買進／賣出／加碼／減碼／建議／目標價」等指示性語句者，除非 `authority` 是 `fininst`（金融機構研究報告），否則會被略過。這是為了避免人為介入 Agent 的決策。
- `published_at` 晚於決策日 19:30 的檔案會被略過（Agent 不可引用）。
- 不合格的檔案只會被略過並警告，不會讓當天流程失敗；所有檔案都會複製到 `runs/<交易日>/raw/inbox/` 留存。

預覽：`python scripts/ingest_inbox.py 2026-10-27 --prev-date 2026-10-26`
