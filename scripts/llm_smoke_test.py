#!/usr/bin/env python
"""LLM 連線測試：主模型與備援各實際呼叫一次，印出模型 ID、回應、token 用量。

  python scripts/llm_smoke_test.py                 # Gemini（.env 的 GOOGLE_API_KEY、LLM_MODEL、LLM_FALLBACK_MODEL）
  python scripts/llm_smoke_test.py --provider anthropic
會計入單日呼叫上限（runs/state/llm_usage.json），兩次呼叫之間自動等 13 秒。
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.agent.llm import LLMError, make_client  # noqa: E402
from esun_agent.agent.llm_budget import QuotaClient, load_dotenv  # noqa: E402

SYSTEM = "你是連線測試。只輸出一個 JSON 物件，不要其他文字。"
USER = '請輸出 {"ok": true, "model_says": "<用十個字以內說你是誰>"}'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", default=None)
    a = ap.parse_args(argv)
    load_dotenv()
    try:
        q = make_client(a.provider)
    except LLMError as e:
        print(f"錯誤：{e}（請在 .env 設定 API key）", file=sys.stderr)
        return 2
    targets = [("主模型", q.primary)] + ([("備援", q.fallback)] if q.fallback is not None else [])
    rc = 0
    for label, client in targets:
        one = QuotaClient(client, None, max_tries=2, min_interval=q.min_interval, usage_path=q.usage_path)
        print(f"== {label}：{client.model}（provider={client.provider}）")
        try:
            r = one.generate(SYSTEM, USER)
        except LLMError as e:
            print(f"   失敗：{e}")
            rc = 1
            continue
        print(f"   實際模型：{r.model}")
        print(f"   回應：{r.text.strip()[:300]}")
        print(f"   tokens：輸入 {r.input_tokens}、輸出 {r.output_tokens}")
    print(f"今日已用 LLM 呼叫 {q.calls_today()} 次（上限 {q.daily_limit}）；LLM_PROVIDER={os.environ.get('LLM_PROVIDER', 'gemini')}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
