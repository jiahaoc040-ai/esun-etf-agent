# esun-etf-agent

玉山 AI CUP 2026「Agent 基金經理人」參賽程式（ETF「主動台灣核心優選 AI」，集中主題＋事件驅動）。

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                  # 填 TEAM_ID、GOOGLE_API_KEY
python -m pytest -q                                   # 全部測試
python scripts/llm_smoke_test.py                      # 主模型與備援各呼叫一次
python scripts/validate_dplan.py <D-Plan.json> --datapack <資料包.json>  # 單獨驗證一份 D-Plan（或 --ctx <context.json>）
```

專案規則摘要：`CLAUDE.md`。策略宣告：`config/strategy_declaration.json`。待辦：`docs/cloud_tasks.md`。

## 每日操作手冊（初賽 10/26～11/27）

每天只有 SS 能做的事是**下載資料包**、（選擇性）**放收件匣資料**、**看摘要**、**上傳並送出**。決策內容由 Agent 產生，**人不得修改 D-Plan**。

每日流程（後台「文件繳交」頁的 ①～⑤）：

1. **19:00 後下載資料包**：登入參賽者後台 →「文件繳交」→「下載資料包」（DataPack JSON，內容是前一交易日結算後的持股、現金與合規狀態）。
2. **放到 `~/Downloads`**（瀏覽器預設下載位置即可；也可放 `data/datapack/`）。檔名不拘，程式依內容找 `doc_type=DataPack` 且 `trade_date`＝明天交易日的最新一份。
3. **跑 daily_run**：launchd 每晚 20:30 自動執行；也可手動 `python scripts/daily_run.py`。
4. **上傳** `runs/<交易日>/D-Plan_<TEAM_ID>_<交易日>.json`（摘要最後一行「★ 請原封不動上傳此檔」）。
5. 確認檢核結果後**按「送出繳交」**（只上傳、沒按送出＝未繳交）。
6. **到「每日數據 → 當日委託」核對**買賣、代號、張數與摘要的「委託」一致；不符請在 08:55 前修正重送。

| 時間 | 誰 | 做什麼 |
|---|---|---|
| 19:00 後 | SS | 後台下載資料包 → 放 `~/Downloads`（步驟 1、2） |
| 20:30 前 | SS（選擇性） | 把新聞／報告放進 `data/inbox/<明天交易日>/`（見下方「收件匣」） |
| 20:30 | launchd | `daily_run` 自動產生明天的 D-Plan（週日～週四晚上；也可手動執行） |
| 20:30 後 | SS | 看摘要 → 上傳 → 送出繳交 → 當日委託核對（步驟 4～6） |
| 06:50 | launchd | `daily_run --morning`：美股 SOX 或台指期夜盤 \|變動\| > 2% 才重產 → 若有，重新上傳並送出 |
| 08:55 前 | SS | 確認送出的是最新一版 |

### 資料包（DataPack）怎麼被使用

- daily_run 會把找到的資料包複製到 `runs/<交易日>/raw/datapack.json`，與自記帳**逐檔對帳**，差異列在摘要與 log，**一律以資料包為準**。
- 帳務口徑：分母 `account.nav_tradable`（不是 `aum`）、現金 `account.cash`、持股 `holdings[].shares`（`receivable_shares` 應收配股不可賣、不計權重與檔數）。
- 摘要最上方會列出資料包的違規／提示（`compliance.incidents` 的 warn／fail）；前一日委託整批作廢時列出 `void_reasons`，被排除的代號列出 `excluded_codes`。
- **「結算尚未產生，請稍後重新下載」**（`missing` 含 `prev_settlement` 或 `account` 為 null）：不產生 D-Plan——過一陣子重新下載資料包再重跑，**不可當成空手**。
- **找不到資料包**：仍會用自記帳產生 D-Plan，但摘要大字標示「**不可上傳**」（自記帳若與後台不同，賣出超過庫存會整份拒收）。請下載資料包後重跑。
- 資料包的 `team_id` 必須等於 `.env` 的 `TEAM_ID`、`trade_date` 必須等於這次的交易日；不符會停止。
- 第一個交易日（`as_of_date` 為 null 且 `missing` 為空）：全現金建倉。
- 週日晚上產生週一的 D-Plan：資料包的 `trade_date` 是週一、`as_of_date` 是週五。

### 收件匣（選擇性）

把新聞或報告（.md／.txt／.pdf／.html）放進 `data/inbox/<明天交易日>/`，每份附同名 `.meta.json`
（`url`、`authority`、`published_at`、`title`）。格式與規則見 `data/inbox/README.md`。
只接受事實性內容：含「買進／賣出／加碼／減碼／建議」等字眼且來源不是金融機構報告的檔案會被略過。預覽：
`python scripts/ingest_inbox.py <交易日> --prev-date <今天>`。

### 確認 daily_run 摘要

排程自動執行；要手動跑：`python scripts/daily_run.py`（`--force` 可在非前一晚執行、`--trade-date` 指定交易日）。
結束時會印出摘要（也存在 `runs/<交易日>/raw/summary_evening.txt`）：

- 最上方：資料包的違規／提示紀錄（有才印）
- 資料包：nav_tradable、現金、持股檔數、前一日委託結果（作廢原因、被排除代號）
- 模式：`Agent 正常產生`，或降級（`凍結既有名單`／`合規基準 A`，原因在 `raw/llm/`）
- 委託後持股、現金比、Active Share（官方門檻 15%）、每筆委託
- `ERROR 0` 才會輸出 D-Plan；有 ERROR 時**不會產生檔案**，請看 log
- 警告：資料來源失敗（降級為缺值）、對帳差異、TEAM_ID 未設定、提交次數…

合規保險（程式自動處理）：前日結算權重 > 9.5%（2330 > 24%）或資料包提示個股權重異常 → 當日強制減碼（推論寫明「被動超限，依規則於 5 日內調整」）；
BUY/ADD 的目標權重以前日收盤 ×1.10（漲停）壓力測試仍須 ≤ 10%（2330 ≤ 25%）；前日結算股數為 0 才用 BUY，否則 ADD。

### 上傳 D-Plan 並送出

摘要最後一行「★ 請原封不動上傳此檔」就是要上傳的路徑：`runs/<交易日>/D-Plan_<TEAM_ID>_<交易日>.json`。
**不要打開修改**（人工修改違反比賽規則，也可能破壞引用鏈與委託公式而被拒收）。上傳後按「送出繳交」，再到「每日數據 → 當日委託」核對。
清晨若印出「D-Plan 已重新產生——請重新上傳」，請在 08:55 前再上傳並送出一次（每日送出上限 25 次，程式會計數）。

### ETF 投資策略說明文件（PDF）

`python scripts/make_strategy_doc.py` → `output/strategy_doc.docx`（保留）＋ `output/strategy_doc.pdf`（上傳到後台「文件繳交 → ETF 投資策略說明文件」，主辦方只收 PDF）。
有 LibreOffice 時用它轉檔；沒有時用 reportlab 並內嵌中文字型（macOS 的 Arial Unicode 等；可用 `ESUN_PDF_FONT` 指定 .ttf/.ttc）。

### 出狀況時

| 狀況 | 處理 |
|---|---|
| 「結算尚未產生，請稍後重新下載」 | 主辦方結算還沒好：稍後重新下載資料包放 `~/Downloads`，重跑 `python scripts/daily_run.py` |
| 摘要顯示「不可上傳」 | 沒找到當天的資料包：下載後重跑 |
| 「資料包 trade_date／team_id／as_of_date 不符」 | 下載到舊檔或別隊的檔：重新下載；確認 `.env` 的 `TEAM_ID` |
| 摘要顯示 `ERROR`、沒有 D-Plan | 看 `runs/<交易日>/raw/daily_run.log`；修正後重跑 `python scripts/daily_run.py --force` |
| 「行情抓取失敗且本機沒有資料」 | 交易所資料晚更新：過 30 分鐘再重跑 |
| 降級（凍結／合規基準 A） | 仍是合規 D-Plan，可直接上傳；原因在 `raw/llm/`（額度、key、輸出不合格） |
| LLM 額度用完 | 自動改用備援模型；仍失敗則降級。檢查：`python scripts/llm_smoke_test.py` |
| 某個資料來源失敗（美股、夜盤、MOPS） | 只會少一則 observation，不影響產生 |
| 當日委託與摘要不符 | 確認上傳的是最新一版、且有按「送出繳交」；08:55 前可重送 |
| Mac 睡著錯過排程 | 醒來後 launchd 會補跑；超過 08:55 就來不及——見 `scripts/launchd/README.md` 的 pmset 設定 |

### 一次性設定

1. `.env`：`TEAM_ID`、`GOOGLE_API_KEY`（Google AI Studio 免費方案），模型 ID 以 AI Studio 為準（`LLM_MODEL`、`LLM_FALLBACK_MODEL`）。
2. 回補資料（第一次）：`python scripts/backfill_market.py`、`python scripts/fetch_shares.py`、`python scripts/fetch_etf_holdings.py --save-raw`。
   月營收「創新高」需要本機有 24 個月歷史：`python scripts/collect_data.py revenue-backfill --start 2024-09 --save-raw`；
   休市日：`python scripts/collect_data.py holidays`（daily_run 缺資料時也會自動抓）。
3. 排程：`bash scripts/launchd/install.sh`，並依 `scripts/launchd/README.md` 設定 pmset。
4. 試跑：`python scripts/daily_run.py --force`，確認摘要與 `runs/<交易日>/` 的輸出。
