# 雲端工作階段任務清單

每個任務各開一個雲端工作階段（claude.ai/code → 選這個 repo），把「Prompt」整段貼上即可。
做完先 review PR、merge，再開下一個。依序做：T1 → T2 → T4 → T3 → T5，T6 最晚 10/20 前完成。

> 雲端環境的網路可能有白名單。如果 TWSE/TPEx/投信網站連不到，請 Claude 用 `tests/fixtures/` 的假資料把程式和測試寫完，真實抓取改在本機或 GitHub Actions 執行。

---

## T1 資料層：上市櫃日行情（含成交均價）

**Prompt：**
先讀 CLAUDE.md。實作 `esun_agent/data/market.py`：
1. 抓上市日行情：TWSE OpenAPI `https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL`（欄位含收盤價、成交股數、成交金額），以及歷史日行情（TWSE `exchangeReport/STOCK_DAY` 依月份查詢）。
2. 抓上櫃日行情：TPEx OpenAPI `https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes`。
3. 只保留 `data/reference/universe_150.csv` 的 150 檔；計算成交均價 avg_price = 成交金額 ÷ 成交股數。
4. 每天存一份 `data/market/YYYY-MM-DD.parquet`（欄位：date, ticker, open, high, low, close, volume, value, avg_price），並回傳 D-Plan source 物件需要的 url、content_as_of（收盤資料用當天 13:30+08:00 或交易所公告時間）和 authority（上市填 twse、上櫃填 tpex）。
5. 寫 `scripts/backfill_market.py`，回補 2025-01-01 至今的資料供回測使用。
6. 加上三大法人買賣超（TWSE `fund/T86`、TPEx 對應 API）。
連不到網路時，用 fixture 假資料把 parser 和測試寫完。所有測試必須全綠。

## T2 主動型 ETF 前 10 大持股（Active Share 用）

**Prompt：**
先讀 CLAUDE.md。`data/reference/active_etfs.csv` 有 30 檔主動型 ETF。實作 `esun_agent/data/etf_holdings.py`：
從各投信官網或 TWSE ETF 資訊揭露頁抓每檔 ETF 的前 10 大持股與權重，存成 `data/etf_top10/YYYY-MM-DD.json`，格式為 `{etf_code: {ticker: weight}}`。
海外型 ETF（例如投資美國科技的 00989A、00402A）持股和台股沒有交集，可以標記並跳過。
抓不到的 ETF 要列出清單，並提供一個 CSV 模板讓我手動補資料。
最後串接 `esun_agent.active_share.check_active_share`，寫一個函式：輸入擬定的目標權重，回傳是否通過；若不通過，指出哪幾檔權重需要調整。

## T3 策略與回測

**Prompt：**
先讀 CLAUDE.md。用 T1 的資料實作：
1. `esun_agent/strategy/factors.py`：計算 150 檔的因子，包括 5/20/60 日動能、20 日波動度、法人買賣超、成交值變化。
2. `esun_agent/strategy/portfolio.py`：因子打分後挑出 22–28 檔，產生目標權重。權重要符合 config 的上限並保留緩衝，現金控制在 [1%, 22%]，2330 權重另外處理，最後通過 Active Share 檢查。
   另外加入換手控制：權重偏離小於 1.5% 時不交易（無交易帶），因為每次往返交易成本約 0.6%。
3. `esun_agent/backtest.py`：用 Ledger 模擬每日流程。T 日決策只能使用 T-1 收盤後的資料，以 T 日 avg_price 成交、扣除費稅，用 close 計算 NAV。輸出 NAV、MDD、換手率和違規次數。
4. 回測 2025–2026，挑幾個參數組合比較；回報結果時一併說明過度擬合的風險。

## T4 D-Plan 組裝器（LLM Agent 核心）

**Prompt：**
先讀 CLAUDE.md 與 docs/competition/D-Plan_撰寫指南_v4.2.md。實作 `esun_agent/agent/`：
1. `observations.py`：把行情、法人買賣超、台指期夜盤、美股/ADR、新聞轉成 sources 和 observations（只寫事實，values 只放數字），ID 從 S1、O1 開始連續編號。
2. `llm.py`：可切換 LLM 供應商（Gemini / Anthropic，API key 從環境變數讀取）。LLM 讀取 observations、策略產生的候選權重和公開說明書宣告的投資理念後，輸出 market_view、inferences，以及每檔持股的 decision 或 no_trade 理由。輸出格式要用 JSON 結構化。
3. `assemble.py`：組出完整 D-Plan。orders 一律由 `derive_orders` 計算，LLM 不能寫股數。funding_for 只在缺口真的存在時才標記（依指南 C14）。
4. 寫檔前呼叫 `validate()`。有 ERROR 時，把錯誤訊息回饋給 LLM 重試，最多 3 次；仍失敗就降級為「全部 no_trade、維持部位」的保底 D-Plan（這份也必須通過驗證）。
5. prompt 模板放在 `prompts/` 並納入版本控制；code_version 用 `git:<short sha>`。
用 tests/fixtures 寫一個端到端測試：產出的 D-Plan 必須 0 ERROR。

## T5 每日排程

**Prompt：**
先讀 CLAUDE.md。寫 `scripts/daily_run.py` 和 `.github/workflows/daily.yml`：
- cron 設在台灣時間每個交易日前一天 19:45（主辦方 19:00 更新淨值）；非交易日跳過，交易日曆可抓 TWSE 休市日 API。
- 流程：讀取官方結算庫存與 NAV（格式先定成 `runs/YYYY-MM-DD/official_state.json`，取得方式待確認）→ 與 Ledger 對帳 → 以官方資料為準 → T1 抓資料 → T3 產生候選權重 → T4 組 D-Plan → 驗證 → 輸出 `runs/<date>/D-Plan_<team_id>_<date>.json` 並上傳成 artifact。
- 原始資料和 LLM 回應都留存在 runs/<date>/raw/，供 12/2 審查使用。
- 失敗時發通知（GitHub Actions 失敗 email 即可）。

## T6 ETF 投資策略說明文件（10/21–10/26 繳交）

**Prompt：**
先讀 CLAUDE.md。依照 `docs/competition/玉山挑戰賽_ETF投資策略說明文件_參賽隊伍繳交格式_v1.docx` 的格式，參考範例 PDF，由 Agent 根據 T3 的策略設定和回測結果產生說明文件，內容包括 ETF 名稱、投資主題和投資理念。
寫成 `scripts/make_strategy_doc.py`，輸出 docx。
注意：之後每天 D-Plan 的推論都要能對回這份文件宣告的主題與因子，所以把宣告的主題/因子也存成 `config/strategy_declaration.json`，供 T4 的 prompt 讀取。
