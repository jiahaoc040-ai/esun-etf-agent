# 玉山 AI CUP 2026「Agent 基金經理人」— 專案說明（給 Claude 讀）

## 目標
每個台股交易日自動產出一份 D-Plan JSON（當日 Agent 決策報告及交易書），
繳交時間：前一日 19:30 ～ 當日 08:55；**首次繳交 2026/10/26 19:30 ～ 10/27 08:55**（交易日 10/27 的 D-Plan）。
初賽 2026-10-26 ～ 11-27，扣除例假日 24 個交易日，至少交滿 22 天。
**規則以 `docs/competition/…比賽辦法_1005更新.pdf` 為準（取代舊版辦法）；格式以 `docs/competition/v4.3/` 為準（schema v4.3、DataPack 規格書 v4.3）。**
**決策必須由 Agent 自主產生，人不得修改內容。**

## 權威文件（docs/competition/）
- `v4.3/DataPack_規格書_v4.3.md`：每日資料包（前日結算帳務、委託結果、合規紀錄）← 帳務唯一來源
- `v4.3/D-Plan.schema_v4.3.json`：結構驗證（同一份放在 esun_agent/ 供程式讀取；輸出 schema_version "4.3"）
- `v4.3/D-Plan_TEAM_042_2026-10-28.json` 正例（0 ERROR）、`v4.3/D-Plan_TEAM_043_2026-10-28.json` 反例（C1／C2／C12）
- `v4.3/後台操作說明.pdf`：下載資料包 → 上傳 → 送出繳交 → 當日委託核對
- `D-Plan_撰寫指南_v4.2.md`：每日繳交格式與檢查項目（repo 內尚無 v4.3 版指南；§⑥ 委託公式分母改為資料包 nav_tradable）
- `AI CUP 2026…比賽辦法.pdf`、`…ETF投資策略說明文件_參賽隊伍繳交格式_v1.docx`、範例 PDF

## 硬規則（違反 = 拒收或記警告，三次警告取消資格）
- 只能持有 data/reference/universe_150.csv 的 150 檔；持股 20–30 檔
- 單檔 ≤ 10%，2330 ≤ 25%；現金 0 ≤ cash ≤ 25%（以 NAV 計；「不超過」25%，內部緩衝不變：1%～22%）
- 整股（1000 股倍數）、只做現股；成交價 = 當日成交均價；手續費 0.1425% 買賣都收、證交稅 0.3% 只賣出
- 我方前 15 大持股 vs 任一主動型 ETF 前 15 大，Active Share 不得 < 15%（連 2 個交易日低於 → 取消資格）
- 除權息：除息現金與除權配發股數都**不在除權息日入帳**，由主辦方期末一次加回 → Ledger 不得在除權日增加股數（只有股票分割／面額變更／減資會調整股數）；每日以資料包 holdings[].shares 為準對帳（receivable_shares 不可賣、不計權重與檔數）
- 帳務分母一律資料包 `account.nav_tradable`（不是 aum）；委託價格＝交易所 T-1 收盤
- 被動超限（價格波動）有 5 個交易日緩衝；當日加碼後超限＝當日違規 → BUY/ADD 以 T-1 收盤 ×1.10 壓力測試仍須 ≤ 10%（2330 ≤ 25%）；action=BUY 只用在前日結算 shares 為 0
- orders 必須由 `esun_agent.orders.derive_orders` 機械產生，不可手調（C2）
- 賣出股數 ≤ 主辦方後台結算庫存（C13，超賣整份拒收）
- market_view.posture 是承諾：委託淨流向與現金區間要對得上（C12）
- 引用鏈 order→decision→inference→observation→source 不可斷（C1）；每檔昨日持股必須出現在 decisions 或 no_trade_decisions
- 改參數或 prompt 模板 = 改版，agent_metadata.code_version 要能對應到 git commit

## 程式結構
- `esun_agent/config.py` 規則常數與自訂緩衝
- `esun_agent/universe.py` 150 檔與主動型 ETF 名單
- `esun_agent/orders.py` 官方委託公式
- `esun_agent/ledger.py` 記帳（成交、NAV、與官方庫存對帳；除權息不入帳、分割才調股數）
- **每日流程（T5，在 SS 的 Mac 上跑）**
  - `esun_agent/collect/`：`calendar.py`（TWSE 休市日）、`revenue.py`（MOPS 月營收 sii/otc，YoY/MoM、24 個月新高，存 `data/revenue/`）、`news.py`（MOPS 當日重大訊息，限 150 檔、發言時間 ≤ D 19:30；上櫃 TPEx OpenAPI t187ap04_O，非 JSON 回應只記單行警告）、`taifex.py`（台指期夜盤 CSV）、`us.py`（SPX/NDX：FRED（有 `FRED_API_KEY` 走正式 API，否則 fredgraph.csv）；SOX/TSM：Nasdaq API historical（必帶 fromdate/todate、瀏覽器 UA、Accept、Origin/Referer＝nasdaq.com，否則 rCode 400）→ CNBC quote webservice（一次查 .SOX|TSM）→ Yahoo（429 退避 2 次）；Stooq 已移除；source url 用行情頁不放 API 網址，各來源 content_as_of＝該筆收盤日美東 16:00；SOX/TSM 某來源最新收盤早於預期最新美股交易日（`expected_us_close`，美東 16:00 後才算收盤、跳過 NYSE 休市）→ 換下一個來源，全部都舊才用最新的並警告；與另一來源（CNBC）同日日漲跌差 > 1pp 警告；FRED 的 NDX/SPX 晚一個交易日只記 info；真實回應 fixture 在 `tests/fixtures/collect/real/`）、`fred.py`（總經 DGS2／DFF／VIXCLS／T10Y2Y → topic=macro observation；每日即時抓取不帶 realtime 參數；只有回測／歷史明確傳 vintage 才帶 realtime_start/end（ALFRED），且先夾到 FRED 今天＝America/Chicago 日期（台北日期常快一天，帶未來日期會 HTTP 400）；HTTP 錯誤警告含狀態碼與 FRED error_message；content_as_of 用觀測值日期；api_key 一律遮蔽成 ***，不得出現在 source url／警告）、`daily.py`（`collect_day`：每步失敗只警告、降級為缺值；上櫃日行情 OpenAPI 失敗改用 afterTrading/otc 依日期查詢並核對日期）。parser 都有 `tests/fixtures/collect/` 測試；`scripts/collect_data.py --save-raw`
  - `esun_agent/agent/llm_budget.py` `QuotaClient`：Gemini 主模型 `LLM_MODEL`（預設 gemini-3.8-flash）＋備援 `LLM_FALLBACK_MODEL`（gemini-3.5-flash-lite）、間隔 ≥ 13 秒、429/5xx 退避、讀取逾時 60 秒、主模型逾時一次或連 3 次失敗／額度用完改備援、單日 10 次、提示詞 > 200K 先縮減（`conviction_agent.fit_prompts`）、`model_version` 記實際模型；`scripts/llm_smoke_test.py`
  - `esun_agent/datapack.py`：主辦方資料包（取代舊的 runs/official）。`find_datapack`（data/datapack/、~/Downloads/ 中 doc_type=DataPack 且 trade_date=T 的最新檔，複製到 runs/<T>/raw/datapack.json）、`parse_datapack`（team_id／trade_date 必須相符；missing 含 prev_settlement 或 account 為 null → `NotSettledError`「結算尚未產生，請稍後重新下載」，不可當空手；as_of_date null 且 missing 空 → 第一個交易日全現金）、`reconcile`、摘要（void_reasons、excluded_codes、incidents warn/fail 置頂）。找不到資料包 → 自記帳＋大字「不可上傳」
  - `esun_agent/official.py`：自記帳 `runs/state/ledger.json` 推估（`project_to`）與 `team_id()`
  - `esun_agent/daily_run.py`＋`scripts/daily_run.py`（`Paths` 的 market／datapack／downloads 可指到暫存資料夾供測試）（`--morning`、`--force`、`--trade-date`；14:30 前 D 取前一交易日、T＝D 的下一交易日；--trade-date 時 D＝T 的前一交易日；警告依來源分組、每則一行）：輸出 `runs/<T>/D-Plan_<TEAM_ID>_<T>.json`，原始資料／LLM 輸入輸出／log 在 `runs/<T>/raw/`；TEAM_ID 讀環境變數（未設定用 TEAM_TBD）
  - `scripts/launchd/`：週日～週四 20:30 晚上流程、週一～週五 06:50 清晨檢查；操作手冊見 README
- `esun_agent/data/inbox.py` 人工收件匣 `data/inbox/<交易日>/`（文件＋同名 .meta.json → events；缺 meta／url、或非金融機構來源含買進／賣出／加碼／減碼／建議等指示性語句 → 略過並警告；全部複製到 runs/<交易日>/raw/inbox/）。`scripts/ingest_inbox.py` 預覽；daily_run 以 `load_inbox` ＋ `merge_into_inputs` 併入 DayContext.inputs
- `esun_agent/data/etf_holdings.py` 主動型 ETF 持股（MoneyDJ 全部持股頁 Basic0007B → 前 15 大，存 `data/etf_top15/`）
- `esun_agent/active_share.py` Active Share 檢查（前 15 大）
- `esun_agent/dplan_validate.py` 提交前驗證（schema + C1/C2/C6/C9/C11/C12/C13/C14 + 覆蓋）
- **正式策略 v2（集中主題＋事件驅動）**
  - `esun_agent/strategy/candidates.py` 每日候選池（約 30 檔）：投信 5/10 日淨買超（成交值標準化）、20 日相對強度、成交值放大，減主動型 ETF 前 15 大重疊扣分；2330 固定列入。參數 `CandidateParams`
  - `esun_agent/strategy/conviction.py` 權重換算（高 8%／中 5.5%，2330 高 15%／中 10%，單檔上限 8%、2330 20%）、湊檔數部位（市值前 30 大各 1–2%，低重疊優先，總持股 23–24，比 20 檔下限多 3 檔緩衝）、現金 2–5%、Active Share 修正（內部 ≥ 20%）、無交易帶 1.5%、換股紀律 `discipline`（持有 ≥ 3 日、每日最多換 4 檔、停損 −12%、事件前一日凍結）。參數 `ConvictionParams`
  - `config/event_calendar.json` 美國重大事件（每筆附官方 url；confirmed=false 未核對、estimated=true 推估；`to_confirm` 是尚未填日期的事件，程式忽略）。程式強制凍結只限 fomc／nvidia_earnings／us_election（`BLACKOUT_KINDS`）；其他事件（CPI、PCE、非農、科技巨頭財報）由 `us_event_window` 標出前 1 日／當日，提示詞要求降低新建倉；VIX > 25 或 DGS2 單日 > 15bp 為風險訊號（`macro_risk_flags`）
  - `esun_agent/agent/conviction_agent.py` 正式 D-Plan 組裝器 `assemble_conviction_day`：LLM 只輸出 market_view、picks（10–12 檔＋high/medium＋obs_ids）、exits；程式換算權重；最多 3 次重試，失敗 → 凍結既有名單 → 再不行改用合規基準 A。狀態 `runs/state/conviction.json`（`conviction_state.py`）
  - `esun_agent/conviction_backtest.py`＋`scripts/run_conviction_backtest.py` 代理回測（候選池前 11 名代替 LLM；預設遲滯：已持有且名次 ≤ 30 續抱）→ `docs/conviction_backtest.md/.json`
- **降級保底（舊版 v1）**：`esun_agent/strategy/final.py` 合規基準 A（`base_weights`／`FinalStrategy.decide`）＋ tilt，參數 `FinalParams`（Active Share 目標 22%、前日 < 17% 強制再平衡、已 < 15% 補到 29%，校準見 `docs/active_share_calibration.md`）；`agent/assemble.py`（`assemble_day`）與 `tilt_state.py` 保留可用
- `esun_agent/agent/` 共用：`observations.py`（資料→sources/observations，含 candidate_screen／trust_flow／position_pnl／filler_universe／event_calendar）、`llm.py`（Gemini 預設／Anthropic；`parse_picks` v2、`parse_proposal` v1）、`plan.py`（目標權重→decisions/orders/posture/funding_for；單日異動 > 60 檔時延後一部分（schema v4.3 上限）；合規保險：`force_passive_trims`（前日權重 > 9.5%／2330 > 24% 或資料包權重提示 → 強制 TRIM，推論寫「被動超限，依規則於 5 日內調整」，不延後）、`stress_cap_buys`（漲停 ×1.10 壓力測試））
- `prompts/` LLM 提示詞模板（`conviction_*.md` 為正式版；改任何模板 = 改版）
- `config/strategy_declaration.json` 策略宣告 v2.3.0（ETF「主動台灣焦點主題」，status=final；`factors` 含 us_events／macro／retail_attention（散戶關注度只能升降一級信心、不單獨觸發買賣），有測試）：`portfolio_rules`＝`ConvictionParams`、`candidate_rules`＝`CandidateParams`、`fallback.portfolio_rules`＝`FinalParams`（有測試）
- Active Share（v2）：內部目標 ≥ 20%；前日收盤 < 17% → 當日強制再平衡；前日已 < 15% → 補到 ≥ 25%
- `docs/conviction_backtest.md`（v2）、`docs/final_strategy_backtest.md`、`docs/backtest_T3.md`（v1 舊版策略，現為降級保底）回測與歸因報告
- `scripts/validate_dplan.py` CLI（`--ctx` 或 `--datapack`）
- `esun_agent/strategy_doc.py`＋`scripts/make_strategy_doc.py`：策略說明文件 docx（保留）＋ PDF（主辦方只收 PDF；LibreOffice 優先，否則 reportlab 內嵌中文 TrueType 字型，`ESUN_PDF_FONT` 可指定）
- `tests/` — 修改任何東西後執行 `python -m pytest -q`，必須全綠

## 開發守則
- 任何產出的 D-Plan 在寫檔前都要跑 `validate()`，有 ERROR 就不得輸出
- 時間一律 `+08:00`；ID 從 1 連號、無前導零
- 新功能都要附測試；不要改動 docs/competition/ 內的官方檔案
- 測試隔離（tests/conftest.py）：不讀真實 .env（`llm_budget.DOTENV_PATH`）、data/etf_top15 固定用 tests/fixtures/etf_top15/、資料包搜尋路徑指到空資料夾；需要「本機沒有行情」時用 `Paths.market` 指到暫存資料夾
- 剩餘待辦見 docs/cloud_tasks.md
