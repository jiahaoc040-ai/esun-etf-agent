# 資料包（DataPack）規格書

每個交易日可下載的 JSON 檔，內容是你的隊伍在**最近一次完成結算後**的部位與合規狀態。

**怎麼取得**：登入參賽者後台 →「文件繳交」頁 → 點「下載資料包」。

---

## 1. 單位與格式

| 項目 | 格式 |
|---|---|
| 金額 | number，單位為元 |
| 比率 | number，`0.1125` 表示 11.25% |
| 股數 | number，整數 |
| 日期 | `YYYY-MM-DD` |
| 時間 | ISO 8601 **台北時間**（`+08:00`），例 `2026-10-27T05:12:08+08:00` |

---

## 2. 欄位

### 2.1 頂層

| 欄位 | 型別 | 內容 |
|---|---|---|
| `schema_version` | string | 規格版本 |
| `doc_type` | string | 固定為 `DataPack` |
| `team_id` | string | 你的隊伍代號（AI CUP 配發） |
| `trade_date` | date | 本檔對應的交易日，即接下來要繳交的那一個交易日。繳交截止後即指向下一個交易日 |
| `as_of_date` | date \| null | `account` 與 `holdings` 實際反映的交易日。`null` 代表尚無結算資料 |
| `generated_at` | datetime | 本檔產生時間 |
| `missing` | string[] | 系統端缺少的資料源，見 §2.2 |
| `prev_day_orders` | object | 前一交易日的委託結果，見 §2.3 |
| `account` | object \| null | 帳戶狀態，見 §2.4 |
| `holdings` | array \| null | 持股明細，見 §2.5 |
| `compliance` | object | 合規狀態，見 §2.6 |

**`as_of_date` 是「這份檔案記錄的部位資料算到哪一個交易日」。**

- **日期**：該交易日結算已完成，`account` 與 `holdings` 是該日收盤結算後的結果。
  `as_of_date` 有日期時**會是 `trade_date` 的前一個交易日**。
- **`null`**：尚無結算資料，表示此時系統為第一個交易日（`account` 是全現金、0 檔的**真值**），
  或前一交易日的結算尚未產生（`missing` 含 `prev_settlement`，此時 **`account` 整段為 `null`**，
  請於結算產生後重新下載）。

### 2.2 `missing`

缺少的資料源。資料齊備時為空陣列。

| 值 | 意思 | 連帶影響 |
|---|---|---|
| `prev_settlement` | 前一交易日的結算尚未產生 | **`account` 整段為 `null`**、`as_of_date` 為 `null`、`holdings` 為 `null` |
| `prev_close` | 收盤價尚未匯入 | 上傳後的檢核會把部分項目標為未驗（`skipped`） |

含 `prev_close` 時，`account.cash_ratio_tradable` 為 `null`（含 `prev_settlement` 時整個 `account` 為 `null`，不會有這個欄位）。

### 2.3 `prev_day_orders`

| 欄位 | 型別 | 內容 |
|---|---|---|
| `outcome` | string | 四個值之一，見下表 |
| `void_reasons` | string[] | 整批作廢的全部原因。`outcome` 為 `voided` 時至少一筆，其餘為空陣列 |
| `excluded_codes` | string[] | 被排除、未成交，但其餘委託照常成交的證券代號 |

`outcome` 的四個值：

| 值 | 意思 |
|---|---|
| `none` | 前一交易日未繳交，沒有委託 |
| `unknown` | 委託結果尚未判定 |
| `voided` | 已繳交，但當日委託整批作廢，當日委託未成交 |
| `executed` | 有成交 |

`void_reasons` 的七個值：

| 值 | 意思 |
|---|---|
| `insufficient_funds` | 資金不足 |
| `oversell` | 賣出股數超過持股 |
| `no_price` | 委託標的缺當日成交均價，且不在標的池內 |
| `compliance_fail` | 合規檢查未通過：**持股檔數／標的池限制／現金部位／當日加碼個股權重**之一。本檔不細分是哪一項，請於「合規狀態」頁查看 |
| `dplan_reference_chain` | 文件引用鏈不完整 |
| `dplan_decision_coverage` | 決策覆蓋不完整 |
| `dplan_round_lot` | 委託非整股 |

⚠️ 繳交當下檢核，未通過檢核導致無法上傳檔案的原因不會列在此處。

`outcome` 為 `executed` 時 `excluded_codes` 仍可能非空：該檔缺當日成交均價、僅排除該檔，其餘委託照常成交。

### 2.4 `account`

`as_of_date` 收盤結算後的狀態。

⚠️ **`missing` 含 `prev_settlement` 時，`account` 整段為 `null`。** 那代表系統還沒有你的部位資料，請等待結算完成後重新下載。

| 欄位 | 型別 | 單位 | 內容 |
|---|---|---|---|
| `cash` | number | 元 | 可動用現金（不含應收配息）。⚠️ **不等於當日買進上限**，交割以淨額計，同日賣出款也計入結算 |
| `cash_ratio_tradable` | number \| null | **比率** | `cash ÷ nav_tradable`。`missing` 非空時為 `null` |
| `nav_tradable` | number | 元 | 可交易淨值 = `cash` + Σ(`holdings[].shares` × `holdings[].close_price`)。計算現金比與個股權重的分母 |
| `aum` | number | 元 | 總資產，另含應收配息與應收配股市值。**不是** `nav_tradable`，不可用於計算比率 |
| `receivable_cash` | number | 元 | 已生效的應收配息。計入 `aum`，賽期內不會到帳，不可用於下單 |
| `holdings_count` | number | 檔 | 持股檔數，只計 `shares > 0` 的檔。與 `holdings` 的陣列長度不一定相同 |

### 2.5 `holdings`

`as_of_date` 收盤結算後的部位，逐檔一列。`shares` 與 `receivable_shares` 之一不為 0 即列出。

⚠️ **`missing` 含 `prev_settlement` 時整段為 `null`**（同 `account`）。**空陣列只在成功建倉前，代表真的空手**，`null` 與空陣列不是同一回事，不要把 `null` 當成「你沒有部位」。

| 欄位 | 型別 | 單位 | 內容 |
|---|---|---|---|
| `code` | string | — | 證券代號 |
| `shares` | number | 股 | 庫存持股。賣出不得超過此數 |
| `receivable_shares` | number | 股 | 應收配股。賽期內不可賣、不計入 `holdings_count` 與權重 |
| `close_price` | number \| null | 元 | `as_of_date` 的收盤價。`null` 表示該檔查無行情，系統計算 `nav_tradable` 時以 0 計入 |
| `tradable_weight` | number \| null | **比率** | 個股權重，分母為 `nav_tradable`。`null` 表示不適用：該檔只有應收配股，或無收盤價 |

### 2.6 `compliance`

| 欄位 | 型別 | 單位 | 內容 |
|---|---|---|---|
| `violations` | number | 天 | 違規天數（累計）。取消資格的依據。對應後台右上角的「警告次數」 |
| `notices` | number | 列 | 異常提示列數（累計）。不計入取消資格。對應後台的「異常提示紀錄」 |
| `incidents` | array | — | 違規與異常提示紀錄，逐筆一列 |

`incidents[]`：

| 欄位 | 型別 | 內容 |
|---|---|---|
| `date` | date | 發生的交易日 |
| `check_code` | string | 檢查項目代號 |
| `status` | string | `warn`（計入 `notices`）或 `fail`（計入 `violations`） |
| `message` | string | 判定說明 |

---

## 3. 範例

### 3.1 一般情形：已結算、有部位

```json
{
  "schema_version": "3.0",
  "doc_type": "DataPack",
  "team_id": "TEAM_042",
  "trade_date": "2026-10-28",
  "as_of_date": "2026-10-27",
  "generated_at": "2026-10-27T18:58:18+08:00",
  "missing": [],
  "prev_day_orders": { "outcome": "executed", "void_reasons": [], "excluded_codes": [] },
  "account": {
    "cash": 135237741.0,
    "cash_ratio_tradable": 0.135265,
    "nav_tradable": 999795141.0,
    "aum": 999795141.0,
    "receivable_cash": 0.0,
    "holdings_count": 22
  },
  "holdings": [
    { "code": "1519", "shares": 21000, "receivable_shares": 0, "close_price": 696.0, "tradable_weight": 0.014619 },
    { "code": "2049", "shares": 107000, "receivable_shares": 0, "close_price": 339.5, "tradable_weight": 0.036334 },
    { "code": "2059", "shares": 1000, "receivable_shares": 0, "close_price": 13100.0, "tradable_weight": 0.013103 },
    { "code": "2308", "shares": 32000, "receivable_shares": 0, "close_price": 2050.0, "tradable_weight": 0.065613 },
    { "code": "2317", "shares": 267000, "receivable_shares": 0, "close_price": 256.0, "tradable_weight": 0.068366 },
    { "code": "2330", "shares": 73000, "receivable_shares": 0, "close_price": 2585.0, "tradable_weight": 0.188744 },
    { "code": "2345", "shares": 10000, "receivable_shares": 0, "close_price": 2050.0, "tradable_weight": 0.020504 },
    { "code": "2376", "shares": 79000, "receivable_shares": 0, "close_price": 380.0, "tradable_weight": 0.030026 },
    { "code": "2454", "shares": 12000, "receivable_shares": 0, "close_price": 4920.0, "tradable_weight": 0.059052 },
    { "code": "2881", "shares": 296000, "receivable_shares": 0, "close_price": 154.5, "tradable_weight": 0.045741 },
    { "code": "2882", "shares": 331000, "receivable_shares": 0, "close_price": 112.0, "tradable_weight": 0.03708 },
    { "code": "2884", "shares": 334000, "receivable_shares": 0, "close_price": 45.45, "tradable_weight": 0.015183 },
    { "code": "2887", "shares": 416000, "receivable_shares": 0, "close_price": 41.45, "tradable_weight": 0.017247 },
    { "code": "2890", "shares": 499000, "receivable_shares": 0, "close_price": 45.4, "tradable_weight": 0.022659 },
    { "code": "2891", "shares": 222000, "receivable_shares": 0, "close_price": 68.4, "tradable_weight": 0.015188 },
    { "code": "3017", "shares": 10000, "receivable_shares": 0, "close_price": 3685.0, "tradable_weight": 0.036858 },
    { "code": "3037", "shares": 13000, "receivable_shares": 0, "close_price": 1310.0, "tradable_weight": 0.017033 },
    { "code": "3231", "shares": 274000, "receivable_shares": 0, "close_price": 189.5, "tradable_weight": 0.051934 },
    { "code": "3443", "shares": 2000, "receivable_shares": 0, "close_price": 8375.0, "tradable_weight": 0.016753 },
    { "code": "3661", "shares": 6000, "receivable_shares": 0, "close_price": 4180.0, "tradable_weight": 0.025085 },
    { "code": "3711", "shares": 57000, "receivable_shares": 0, "close_price": 744.0, "tradable_weight": 0.042417 },
    { "code": "6669", "shares": 11000, "receivable_shares": 0, "close_price": 2290.0, "tradable_weight": 0.025195 }
  ],
  "compliance": { "violations": 0, "notices": 0, "incidents": [] }
}
```

### 3.2 尚無結算資料：`as_of_date` 為 `null` 的兩種情形

**(a) 第一個交易日** —— `account` 是**真值**（全現金、0 檔），`holdings` 是空陣列：

```json
{
  "schema_version": "3.0",
  "doc_type": "DataPack",
  "team_id": "TEAM_042",
  "trade_date": "2026-10-27",
  "as_of_date": null,
  "generated_at": "2026-10-27T05:12:08+08:00",
  "missing": [],
  "prev_day_orders": { "outcome": "none", "void_reasons": [], "excluded_codes": [] },
  "account": {
    "cash": 1000000000.0, "cash_ratio_tradable": 1.0, "nav_tradable": 1000000000.0,
    "aum": 1000000000.0, "receivable_cash": 0.0, "holdings_count": 0
  },
  "holdings": [],
  "compliance": { "violations": 0, "notices": 0, "incidents": [] }
}
```

**(b) 前一交易日結算尚未產生** —— `account` 與 `holdings` **整段為 `null`**，請於結算產生後重新下載：

```json
{
  "schema_version": "3.0",
  "doc_type": "DataPack",
  "team_id": "TEAM_042",
  "trade_date": "2026-10-29",
  "as_of_date": null,
  "generated_at": "2026-10-28T18:58:11+08:00",
  "missing": ["prev_settlement"],
  "prev_day_orders": { "outcome": "unknown", "void_reasons": [], "excluded_codes": [] },
  "account": null,
  "holdings": null,
  "compliance": { "violations": 0, "notices": 0, "incidents": [] }
}
```

### 3.3 除權配股：`shares` 為 0 也可能仍有部位

配股待入帳的檔位會留在 `holdings` 裡。`tradable_weight` 為 `null`（不適用，不是 0）：

```json
{ "code": "6669", "shares": 0, "receivable_shares": 9913,
  "close_price": 2610.0, "tradable_weight": null }
```

同一檔若還留有可交易部位，`shares` 與 `receivable_shares` 會同時不為 0：

```json
{ "code": "6669", "shares": 3000, "receivable_shares": 9913,
  "close_price": 2610.0, "tradable_weight": 0.00783 }
```

### 3.4 有違規與異常提示紀錄

```json
"compliance": {
  "violations": 1,
  "notices": 2,
  "incidents": [
    { "date": "2026-10-28", "check_code": "oversell", "status": "fail",
      "message": "賣出 5,000 股超過庫存 3,000 股" },
    { "date": "2026-10-29", "check_code": "cash_ratio", "status": "warn",
      "message": "預估現金比 26.4%，超過上限" }
  ]
}
```

`status` 為 `fail` 的列計入 `violations`（違規天數，取消資格的依據）；`warn` 的列計入 `notices`，不計入取消資格。
