# ETF 持股頁 fixture

- `real/moneydj_all_<code>.html`：**新格式、真實頁面**（Basic0007B「個別ETF-全部持股」，列出所有持股，含台指期貨／個股期貨 `.TF` 列），2026-10-11 抓下；
  對應 `data/etf_top15/2026-10-11.json`（測試會比對兩者一致）。
- `moneydj_all_holdings.html`：**合成**的新格式頁面，用來測邊界情況（第 16 名截斷、期貨列只靠 `.TF` 代號辨識、fetch／update 流程）。
- `moneydj_sample.html`、`moneydj_old.html`、`moneydj_short.html`、`moneydj_nodetail.html`、`real/moneydj_<code>.html`：**舊格式**（Basic0007「持股明細」，只列前 10 大），
  保留作回歸測試，解析時需傳 `n=10`。
