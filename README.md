# esun-etf-agent

玉山 AI CUP 2026「Agent 基金經理人」參賽程式。

```bash
pip install -r requirements.txt
python -m pytest -q                                   # 全部測試
python scripts/validate_dplan.py <D-Plan.json> --ctx <context.json>   # 提交前驗證
```

目前完成：150 檔/主動型 ETF 名單、官方委託公式、記帳、Active Share、D-Plan 驗證器。
待辦與雲端工作階段 prompt：見 `docs/cloud_tasks.md`。專案規則摘要：`CLAUDE.md`。
