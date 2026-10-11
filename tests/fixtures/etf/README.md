# ETF 持股頁 fixture

- `moneydj_all_holdings.html`：**新格式**（Basic0007B「個別ETF-全部持股」，列出所有持股、含現金與台指期貨列）。合成檔，結構依舊頁面推得；
  拿到真實頁面後請用 `scripts/fetch_etf_holdings.py --save-raw` 存下並加進 `real_b/`。
- `moneydj_sample.html`、`moneydj_old.html`、`moneydj_short.html`、`moneydj_nodetail.html`、`real/`：**舊格式**（Basic0007「持股明細」，只列前 10 大），
  保留作回歸測試，解析時需傳 `n=10`。
