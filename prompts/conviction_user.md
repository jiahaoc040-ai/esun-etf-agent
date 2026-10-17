# 決策日資料
- 交易日（要繳交的 D-Plan）：{{trade_date}}；資料截止（決策日）：{{prev_date}}
- 提示詞版本：{{prompt_version}}
- {{blackout}}
- {{us_events}}
- {{macro_risk}}
- {{market_flow}}
- {{restrictions}}

# 本 ETF 宣告（節錄）
{{declaration_summary}}

# 目前高信心名單（預設續抱，續抱不必有新證據；移出或降級請看規則 2、3、7）
{{current_picks}}

# 候選池（程式依投信 5／10 日淨買超、20 日相對強度、成交值放大打分，並對主動型 ETF 重疊扣分；依名次排列）
{{candidates}}

# 可引用的 observations（只能引用這些 obs_id）
{{observations}}

{{feedback}}

請依 system 指示輸出 JSON。
