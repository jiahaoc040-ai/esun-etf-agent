"""每日資料收集（在 SS 的 Mac 上跑；雲端連不到 TWSE／MOPS／TAIFEX）。

各模組都分成「純函式 parser（tests/fixtures/collect/ 測試）」與「抓取（HTTP，原始回應存 raw_dir）」兩層；
任何來源失敗只記警告、降級為缺值，不中斷每日流程（daily.collect_day 統一處理）。
"""
