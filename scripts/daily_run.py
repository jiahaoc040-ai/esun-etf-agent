#!/usr/bin/env python
"""每日產生 D-Plan（在 SS 的 Mac 上跑；launchd 排程見 scripts/launchd/）。

  python scripts/daily_run.py                   # 晚上：收集 → 帳務對帳 → Agent → 驗證 → runs/<T>/D-Plan_<team>_<T>.json
  python scripts/daily_run.py --morning         # 清晨：SOX 或台指期夜盤 |變動| > 2% 才重新產生
  python scripts/daily_run.py --trade-date 2026-10-27 --force
設定放在專案根目錄 .env：GOOGLE_API_KEY、TEAM_ID、（選填）LLM_MODEL、LLM_FALLBACK_MODEL、LLM_PROVIDER、ANTHROPIC_API_KEY。
結束碼：0 = 成功或無需動作；1 = 有錯誤、沒有輸出 D-Plan。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.daily_run import main_cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(main_cli())
