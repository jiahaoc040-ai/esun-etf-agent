# esun-etf-agent

玉山 AI CUP 2026「Agent 基金經理人」參賽程式（ETF「主動台灣核心優選 AI」，集中主題＋事件驅動）。

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                  # 填 TEAM_ID、GOOGLE_API_KEY
python -m pytest -q                                   # 全部測試
python scripts/llm_smoke_test.py                      # 主模型與備援各呼叫一次
python scripts/validate_dplan.py <D-Plan.json> --ctx <context.json>   # 單獨驗證一份 D-Plan
```

專案規則摘要：`CLAUDE.md`。策略宣告：`config/strategy_declaration.json`。待辦：`docs/cloud_tasks.md`。

## 每日操作手冊（初賽 10/26～11/27）

每天只有 SS 能做的事是**填官方帳務**、（選擇性）**放收件匣資料**、**看摘要**、**上傳**。決策內容由 Agent 產生，**人不得修改 D-Plan**。

| 時間 | 誰 | 做什麼 |
|---|---|---|
| 19:00 後 | SS | 主辦方更新淨值後，填 `runs/official/<今天日期>.json`（見步驟 1） |
| 20:30 前 | SS（選擇性） | 把新聞／報告放進 `data/inbox/<明天交易日>/`（見步驟 2） |
| 20:30 | launchd | `daily_run` 自動產生明天的 D-Plan（週日～週四晚上；也可手動執行） |
| 20:30 後 | SS | 看摘要、上傳 D-Plan（步驟 3、4） |
| 06:50 | launchd | `daily_run --morning`：美股 SOX 或台指期夜盤 \|變動\| > 2% 才重產 → 若有，重新上傳 |
| 08:55 前 | SS | 確認上傳的是最新一版 |

### 1. 填官方帳務（每個交易日晚上）

```bash
python scripts/new_official_template.py          # 依昨天帳務＋今天委託推估草稿 → runs/official/<今天>.draft.json
```

打開草稿，**逐項對照主辦方後台**修正 `nav`、`cash`、`holdings`（每檔股數），刪除 `status`、`_note` 兩行，另存為
`runs/official/<今天>.json`：

```json
{"team_id": "TEAM_042", "nav": 1003456789.0, "cash": 31234567.0, "holdings": {"2330": 120000, "2317": 300000}}
```

- daily_run 會拿它和自記帳對帳，差異逐條列在摘要與 log，**一律以官方為準**。
- 沒填官方檔時 daily_run 仍會產生 D-Plan，但會大字警告：自記帳若與後台不同，賣出股數超過後台庫存會**整份拒收**（C13）。
- 週日晚上產生週一的 D-Plan 時，用的是週五的官方檔（`runs/official/<週五>.json`）。

### 2. 放收件匣資料（選擇性）

把新聞或報告（.md／.txt／.pdf／.html）放進 `data/inbox/<明天交易日>/`，每份附同名 `.meta.json`
（`url`、`authority`、`published_at`、`title`）。格式與規則見 `data/inbox/README.md`。
只接受事實性內容：含「買進／賣出／加碼／減碼／建議」等字眼且來源不是金融機構報告的檔案會被略過。預覽：
`python scripts/ingest_inbox.py <交易日> --prev-date <今天>`。

### 3. 確認 daily_run 摘要

排程自動執行；要手動跑：`python scripts/daily_run.py`（`--force` 可在非前一晚執行、`--trade-date` 指定交易日）。
結束時會印出摘要（也存在 `runs/<交易日>/raw/summary_evening.txt`）：

- 模式：`Agent 正常產生`，或降級（`凍結既有名單`／`合規基準 A`，原因在 `raw/llm/`）
- 委託後持股、現金比、Active Share（官方門檻 15%）、每筆委託
- `ERROR 0` 才會輸出 D-Plan；有 ERROR 時**不會產生檔案**，請看 log
- 警告：資料來源失敗（降級為缺值）、對帳差異、TEAM_ID 未設定、提交次數…

### 4. 原封不動上傳 D-Plan

摘要最後一行「★ 請原封不動上傳此檔」就是要上傳的路徑：`runs/<交易日>/D-Plan_<TEAM_ID>_<交易日>.json`。
**不要打開修改**（人工修改違反比賽規則，也可能破壞引用鏈與委託公式而被拒收）。
清晨若印出「D-Plan 已重新產生——請重新上傳」，請在 08:55 前再上傳一次（每日提交上限 25 次，程式會計數）。

### 出狀況時

| 狀況 | 處理 |
|---|---|
| 摘要顯示 `ERROR`、沒有 D-Plan | 看 `runs/<交易日>/raw/daily_run.log`；修正後重跑 `python scripts/daily_run.py --force` |
| 「行情抓取失敗且本機沒有資料」 | 交易所資料晚更新：過 30 分鐘再重跑 |
| 降級（凍結／合規基準 A） | 仍是合規 D-Plan，可直接上傳；原因在 `raw/llm/`（額度、key、輸出不合格） |
| LLM 額度用完 | 自動改用備援模型；仍失敗則降級。檢查：`python scripts/llm_smoke_test.py` |
| 某個資料來源失敗（美股、夜盤、MOPS） | 只會少一則 observation，不影響產生 |
| Mac 睡著錯過排程 | 醒來後 launchd 會補跑；超過 08:55 就來不及——見 `scripts/launchd/README.md` 的 pmset 設定 |

### 一次性設定

1. `.env`：`TEAM_ID`、`GOOGLE_API_KEY`（Google AI Studio 免費方案），模型 ID 以 AI Studio 為準（`LLM_MODEL`、`LLM_FALLBACK_MODEL`）。
2. 回補資料（第一次）：`python scripts/backfill_market.py`、`python scripts/fetch_shares.py`、`python scripts/fetch_etf_holdings.py --save-raw`。
   月營收「創新高」需要本機有 24 個月歷史：`python scripts/collect_data.py revenue-backfill --start 2024-09 --save-raw`；
   休市日：`python scripts/collect_data.py holidays`（daily_run 缺資料時也會自動抓）。
3. 排程：`bash scripts/launchd/install.sh`，並依 `scripts/launchd/README.md` 設定 pmset。
4. 試跑：`python scripts/daily_run.py --force`，確認摘要與 `runs/<交易日>/` 的輸出。
