# 玉山 AI CUP 2026「Agent 基金經理人」— 專案說明（給 Claude 讀）

## 目標
每個台股交易日自動產出一份 D-Plan JSON（當日 Agent 決策報告及交易書），
繳交時間：前一日 19:30 ～ 當日 08:55；**首次繳交 2026/10/26 19:30 ～ 10/27 08:55**（交易日 10/27 的 D-Plan）。
初賽 2026-10-26 ～ 11-27，扣除例假日 24 個交易日，至少交滿 22 天。
**規則以 `docs/competition/…比賽辦法_1005更新.pdf` 為準（取代舊版辦法）。**
**決策必須由 Agent 自主產生，人不得修改內容。**

## 權威文件（docs/competition/）
- `D-Plan_撰寫指南_v4.2.md`：每日繳交格式與檢查項目 ← 最重要
- `D-Plan.schema_v4.2.json`：結構驗證（同一份放在 esun_agent/ 供程式讀取）
- `D-Plan_TEAM_042_*.json` 正例、`D-Plan_TEAM_043_*.json` 反例
- `AI CUP 2026…比賽辦法.pdf`、`…ETF投資策略說明文件_參賽隊伍繳交格式_v1.docx`、範例 PDF

## 硬規則（違反 = 拒收或記警告，三次警告取消資格）
- 只能持有 data/reference/universe_150.csv 的 150 檔；持股 20–30 檔
- 單檔 ≤ 10%，2330 ≤ 25%；現金 0 ≤ cash ≤ 25%（以 NAV 計；「不超過」25%，內部緩衝不變：1%～22%）
- 整股（1000 股倍數）、只做現股；成交價 = 當日成交均價；手續費 0.1425% 買賣都收、證交稅 0.3% 只賣出
- 我方前 15 大持股 vs 任一主動型 ETF 前 15 大，Active Share 不得 < 15%（連 2 個交易日低於 → 取消資格）
- 除權息：除息現金與除權配發股數都**不在除權息日入帳**，由主辦方期末一次加回 → Ledger 不得在除權日增加股數（只有股票分割／面額變更／減資會調整股數）；每日以主辦方後台庫存為準對帳
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
- `esun_agent/data/etf_holdings.py` 主動型 ETF 持股（MoneyDJ 全部持股頁 Basic0007B → 前 15 大，存 `data/etf_top15/`）
- `esun_agent/active_share.py` Active Share 檢查（前 15 大）
- `esun_agent/dplan_validate.py` 提交前驗證（schema + C1/C2/C6/C9/C11/C12/C13/C14 + 覆蓋）
- **正式策略 v2（集中主題＋事件驅動）**
  - `esun_agent/strategy/candidates.py` 每日候選池（約 30 檔）：投信 5/10 日淨買超（成交值標準化）、20 日相對強度、成交值放大，減主動型 ETF 前 15 大重疊扣分；2330 固定列入。參數 `CandidateParams`
  - `esun_agent/strategy/conviction.py` 權重換算（高 8%／中 5.5%，2330 高 15%／中 10%，單檔上限 8%、2330 20%）、湊檔數部位（市值前 30 大各 1–2%，總持股 21–24）、現金 2–5%、Active Share 修正（內部 ≥ 20%）、無交易帶 1.5%、換股紀律 `discipline`（持有 ≥ 3 日、每日最多換 4 檔、停損 −12%、事件前一日凍結）。參數 `ConvictionParams`
  - `config/event_calendar.json` FOMC／NVIDIA 財報／美國大選（estimated=true 者要在公告後更新）
  - `esun_agent/agent/conviction_agent.py` 正式 D-Plan 組裝器 `assemble_conviction_day`：LLM 只輸出 market_view、picks（10–12 檔＋high/medium＋obs_ids）、exits；程式換算權重；最多 3 次重試，失敗 → 凍結既有名單 → 再不行改用合規基準 A。狀態 `runs/state/conviction.json`（`conviction_state.py`）
  - `esun_agent/conviction_backtest.py`＋`scripts/run_conviction_backtest.py` 代理回測（候選池前 11 名代替 LLM；預設遲滯：已持有且名次 ≤ 30 續抱）→ `docs/conviction_backtest.md/.json`
- **降級保底（舊版 v1）**：`esun_agent/strategy/final.py` 合規基準 A（`base_weights`／`FinalStrategy.decide`）＋ tilt，參數 `FinalParams`（Active Share 目標 22%、前日 < 17% 強制再平衡、已 < 15% 補到 29%，校準見 `docs/active_share_calibration.md`）；`agent/assemble.py`（`assemble_day`）與 `tilt_state.py` 保留可用
- `esun_agent/agent/` 共用：`observations.py`（資料→sources/observations，含 candidate_screen／trust_flow／position_pnl／filler_universe／event_calendar）、`llm.py`（Gemini 預設／Anthropic；`parse_picks` v2、`parse_proposal` v1）、`plan.py`（目標權重→decisions/orders/posture/funding_for；單日異動 > 30 檔時延後一部分，schema 上限）
- `prompts/` LLM 提示詞模板（`conviction_*.md` 為正式版；改任何模板 = 改版）
- `config/strategy_declaration.json` 策略宣告 v2（ETF「主動台灣核心優選 AI」，status=final）：`portfolio_rules`＝`ConvictionParams`、`candidate_rules`＝`CandidateParams`、`fallback.portfolio_rules`＝`FinalParams`（有測試）
- Active Share（v2）：內部目標 ≥ 20%；前日收盤 < 17% → 當日強制再平衡；前日已 < 15% → 補到 ≥ 25%
- `docs/conviction_backtest.md`（v2）、`docs/final_strategy_backtest.md`、`docs/backtest_T3.md`（v1 舊版策略，現為降級保底）回測與歸因報告
- `scripts/validate_dplan.py` CLI
- `tests/` — 修改任何東西後執行 `python -m pytest -q`，必須全綠

## 開發守則
- 任何產出的 D-Plan 在寫檔前都要跑 `validate()`，有 ERROR 就不得輸出
- 時間一律 `+08:00`；ID 從 1 連號、無前導零
- 新功能都要附測試；不要改動 docs/competition/ 內的官方檔案
- 剩餘待辦見 docs/cloud_tasks.md
