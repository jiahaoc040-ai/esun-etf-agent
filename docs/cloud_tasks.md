# 雲端工作階段任務清單

> **規則更新（比賽辦法 1005 版，取代舊版）**：Active Share 改為我方前 15 大 vs 各主動型 ETF 前 15 大、< 15% 才違規；現金「不超過」25%；
> 除息現金與除權配股都不在除權息日入帳（期末由主辦方一次加回）；首次繳交 2026/10/26 19:30 ～ 10/27 08:55。
> 以下 T1–T6 為原始任務提示（保留歷史），已依新規則實作的部分見 CLAUDE.md。

## 1005 版規則後的狀態

- ETF 持股已改用 MoneyDJ Basic0007B 真實前 15 大（`data/etf_top15/2026-10-11.json`，27 檔；3 檔純海外型跳過），真實頁面 fixture 在 `tests/fixtures/etf/real/moneydj_all_*.html`。
- Active Share 參數已用真實前 15 大重新校準（`docs/active_share_calibration.md`）。**正式開跑前每日要重抓 ETF 持股**（`scripts/fetch_etf_holdings.py`），
  並在開賽前再重跑一次 `scripts/calibrate_active_share.py` 確認參數仍成立（改參數＝改版，換 code_version，並同步 `config/strategy_declaration.json`）。

## v2.3.0 待 SS 在 Mac 上確認的美國事件日期（雲端連不到官方網站）

確認後把日期填進 `config/event_calendar.json` 的 `events`（kind、name、url 照抄 `to_confirm`，加 `"confirmed": true`），並從 `to_confirm` 移除；
已在 `events` 的項目核對後把 `confirmed` 改成 true。改本檔＝改版（換 code_version），改完重跑 `python -m pytest -q` 與 `scripts/make_strategy_doc.py`。

| 事件 | 官方來源 | 狀態 |
|---|---|---|
| FOMC 10/27–28（決議日 2026-10-28） | https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm | 已在 events，confirmed=false |
| 美國期中選舉 2026-11-03 | https://www.fec.gov/introduction-campaign-finance/election-results-and-voting-information/ | 已在 events（法定日期），confirmed=false |
| NVIDIA FY2027 Q3 財報（目前推估 2026-11-18） | https://investor.nvidia.com/events-and-presentations/default.aspx | 已在 events，estimated=true |
| CPI（9 月、10 月數據） | https://www.bls.gov/schedule/news_release/cpi.htm | to_confirm |
| PCE／個人所得與支出 | https://www.bea.gov/news/schedule | to_confirm |
| 非農就業 | https://www.bls.gov/schedule/news_release/empsit.htm | to_confirm |
| Apple 財報 | https://investor.apple.com/investor-relations/default.aspx | to_confirm |
| Microsoft 財報 | https://www.microsoft.com/en-us/investor/events/events-recent | to_confirm |
| Alphabet 財報 | https://abc.xyz/investor/ | to_confirm |
| Amazon 財報 | https://ir.aboutamazon.com/events/default.aspx | to_confirm |
| Meta 財報 | https://investor.atmeta.com/investor-events/default.aspx | to_confirm |
| AMD 財報 | https://ir.amd.com/news-events/ir-calendar | to_confirm |

## 待 SS 在 Mac 上實測的端點（雲端連不到，parser 依 fixture 寫成）

| 端點 | 程式 | 要核對的事 |
|---|---|---|
| https://www.twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD&selectType=ALL&response=json | `MarketFetcher.twse_inst` | stat=OK、欄位名與 `parse_twse_t86` 相符 |
| https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=ALLBUT0999&response=json | `parse_twse_mi_index` | tables 中有標題含「每日收盤行情」的表、date 欄位、fields 名稱 |
| https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate=YYYYMMDD&response=json | `parse_twse_bfi82u` | 列名（外資及陸資(不含外資自營商)、投信、自營商(自行買賣)／(避險)、合計）、是否用 tables |
| https://openapi.twse.com.tw/v1/announcement/punish | `collect/restrictions.py` | 代號欄（Code）、處置期間欄（含 Period）格式 |
| https://openapi.twse.com.tw/v1/announcement/notice | 同上 | 代號欄 |
| https://openapi.twse.com.tw/v1/exchangeReport/TWTAWU | 同上 | 代號欄、恢復交易日欄（含 Resum／恢復） |
| https://www.tpex.org.tw/openapi/v1/tpex_disposal_information | 同上 | 代號欄（SecuritiesCompanyCode）、處置期間欄 |
| https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information | 同上 | 代號欄 |
| FRED DEXTAUS（API／fredgraph.csv） | `collect/fred.py` | 最新觀測日（H.10 每週更新，常落後數日） |
| https://www.federalreserve.gov/feeds/press_all.xml | `collect/fed.py` | item 的 title／link／pubDate／category 格式 |

真實回應請存到 `tests/fixtures/market/real/` 或 `tests/fixtures/collect/real/`，parser 若不符再修。

## v2.4.0 待 SS 在 Mac 上實測的端點

| 端點 | 程式 | 要核對的事 |
|---|---|---|
| https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols=TWD%3D&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json | `collect/fx.py` | last／change／change_pct／last_time 格式（fixture 依 SS 實測值 32.429、+0.087 建立，change_pct 與 last_time 為推測） |
| https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL | `collect/corporate.py` | 代號欄、除權除息日欄名與日期格式 |
| https://openapi.twse.com.tw/v1/opendata/t187ap12_L | 同上 | 代號、申報日期、轉讓股數欄名 |
| https://openapi.twse.com.tw/v1/Announcement/BFZFZU_T | 同上 | 代號欄、日期欄 |
| https://openapi.twse.com.tw/v1/opendata/t187ap09_L | 同上 | 是否為董監質押資料；持股、設質股數、設質比例欄名 |
| POST https://mopsov.twse.com.tw/mops/web/ajax_t100sb02_1（TYPEK=sii／otc、year=115、month=10／11） | `collect/conference.py`、`scripts/fetch_conferences.py` | 表頭「公司代號」「召開法人說明會日期」、日期格式；執行後 commit event_calendar.json |

真實回應請放進 repo 供測試：`data/raw/corp/corp_insider.json` → `tests/fixtures/collect/real/corp_insider.json`、`data/raw/corp/corp_pledge.json` → `tests/fixtures/collect/real/corp_pledge.json`（放入後 `test_real_insider_response`／`test_real_pledge_response` 會自動執行，否則略過）。

上櫃對應端點尚未找到（雲端無法查 TPEx OpenAPI 清單）：除權除息預告、內部人持股轉讓事前申報、投資理財節目異常推介、董監設質比例。
請在 https://www.tpex.org.tw/openapi/ 的 Swagger 清單確認名稱後告知，再加進 `corporate.ENDPOINTS`。

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

## T2 主動型 ETF 持股（Active Share 用；1005 版辦法起改為前 15 大）

**Prompt：**
先讀 CLAUDE.md。`data/reference/active_etfs.csv` 有 30 檔主動型 ETF。實作 `esun_agent/data/etf_holdings.py`：
從各投信官網或 TWSE ETF 資訊揭露頁抓每檔 ETF 的持股與權重（原為前 10 大；1005 版辦法改為取前 15 大，來源改為 MoneyDJ 全部持股頁 Basic0007B），存成 `data/etf_top15/YYYY-MM-DD.json`，格式為 `{etf_code: {ticker: weight}}`。
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

## T5 每日排程（已完成：scripts/daily_run.py、esun_agent/collect/、scripts/launchd/，操作手冊見 README）

**Prompt：**
先讀 CLAUDE.md。寫 `scripts/daily_run.py` 和 `.github/workflows/daily.yml`：
- cron 設在台灣時間每個交易日前一天 19:45（主辦方 19:00 更新淨值）；非交易日跳過，交易日曆可抓 TWSE 休市日 API。
- 流程：讀取官方結算庫存與 NAV（格式先定成 `runs/YYYY-MM-DD/official_state.json`，取得方式待確認）→ 與 Ledger 對帳 → 以官方資料為準 → T1 抓資料 → T3 產生候選權重 → T4 組 D-Plan → 驗證 → 輸出 `runs/<date>/D-Plan_<team_id>_<date>.json` 並上傳成 artifact。
- 原始資料和 LLM 回應都留存在 runs/<date>/raw/，供 12/2 審查使用。
- 人工收件匣：`esun_agent.data.inbox.load_inbox(trade_date, prev_date)` 讀 data/inbox/<trade_date>/，以 `merge_into_inputs` 併入 inputs["events"]；警告寫入當日 log（已實作，見 data/inbox/README.md）。
- 失敗時發通知（GitHub Actions 失敗 email 即可）。

## T6 ETF 投資策略說明文件（10/21–10/26 繳交）

**Prompt：**
先讀 CLAUDE.md。依照 `docs/competition/玉山挑戰賽_ETF投資策略說明文件_參賽隊伍繳交格式_v1.docx` 的格式，參考範例 PDF，由 Agent 根據 T3 的策略設定和回測結果產生說明文件，內容包括 ETF 名稱、投資主題和投資理念。
寫成 `scripts/make_strategy_doc.py`，輸出 docx。
注意：之後每天 D-Plan 的推論都要能對回這份文件宣告的主題與因子，所以把宣告的主題/因子也存成 `config/strategy_declaration.json`，供 T4 的 prompt 讀取。
