# 玉山 AI CUP 2026「Agent 基金經理人」— 專案說明（給 Claude 讀）

## 目標
每個台股交易日自動產出一份 D-Plan JSON（當日 Agent 決策報告及交易書），
繳交時間：前一日 19:30 ～ 當日 08:55。初賽 2026-10-26 ～ 11-27，至少交滿 22 天。
**決策必須由 Agent 自主產生，人不得修改內容。**

## 權威文件（docs/competition/）
- `D-Plan_撰寫指南_v4.2.md`：每日繳交格式與檢查項目 ← 最重要
- `D-Plan.schema_v4.2.json`：結構驗證（同一份放在 esun_agent/ 供程式讀取）
- `D-Plan_TEAM_042_*.json` 正例、`D-Plan_TEAM_043_*.json` 反例
- `AI CUP 2026…比賽辦法.pdf`、`…ETF投資策略說明文件_參賽隊伍繳交格式_v1.docx`、範例 PDF

## 硬規則（違反 = 拒收或記警告，三次警告取消資格）
- 只能持有 data/reference/universe_150.csv 的 150 檔；持股 20–30 檔
- 單檔 ≤ 10%，2330 ≤ 25%；現金 0 ≤ cash < 25%（以 NAV 計）
- 整股（1000 股倍數）、只做現股；成交價 = 當日成交均價；手續費 0.1425% 買賣都收、證交稅 0.3% 只賣出
- 前 10 大持股 vs 任一主動型 ETF 前 10 大，Active Share ≥ 20%（連 2 日低於 → 取消資格）
- orders 必須由 `esun_agent.orders.derive_orders` 機械產生，不可手調（C2）
- 賣出股數 ≤ 主辦方後台結算庫存（C13，超賣整份拒收）
- market_view.posture 是承諾：委託淨流向與現金區間要對得上（C12）
- 引用鏈 order→decision→inference→observation→source 不可斷（C1）；每檔昨日持股必須出現在 decisions 或 no_trade_decisions
- 改參數或 prompt 模板 = 改版，agent_metadata.code_version 要能對應到 git commit

## 程式結構
- `esun_agent/config.py` 規則常數與自訂緩衝
- `esun_agent/universe.py` 150 檔與主動型 ETF 名單
- `esun_agent/orders.py` 官方委託公式
- `esun_agent/ledger.py` 記帳（成交、NAV、與官方庫存對帳）
- `esun_agent/active_share.py` Active Share 檢查
- `esun_agent/dplan_validate.py` 提交前驗證（schema + C1/C2/C6/C9/C11/C12/C13/C14 + 覆蓋）
- `scripts/validate_dplan.py` CLI
- `tests/` — 修改任何東西後執行 `python -m pytest -q`，必須全綠

## 開發守則
- 任何產出的 D-Plan 在寫檔前都要跑 `validate()`，有 ERROR 就不得輸出
- 時間一律 `+08:00`；ID 從 1 連號、無前導零
- 新功能都要附測試；不要改動 docs/competition/ 內的官方檔案
- 剩餘待辦見 docs/cloud_tasks.md
