"""LLM 呼叫預算與容錯（Gemini 免費方案：主模型 5 RPM／20 RPD／250K TPM）。

QuotaClient(primary, fallback)：
  - 兩次呼叫間隔 ≥ LLM_MIN_INTERVAL（預設 13 秒；跨程序，記在 runs/state/llm_usage.json）。
  - 429／5xx／連線錯誤：指數退避（15、30、60 秒…）重試，每次 generate 最多 max_tries 次 HTTP 呼叫。
  - 主模型逾時（Gemini 讀取逾時 60 秒）一次 → 直接改用備援，不再重試主模型。
  - 主模型連續失敗 3 次，或回應顯示當日額度用完（RESOURCE_EXHAUSTED 且為 per-day），或本機紀錄已達 RPD
    → 本次程序之後都改用備援模型。
  - 單日（台北日期）呼叫上限 LLM_DAILY_CALL_LIMIT（預設 10）：每一次 HTTP 呼叫都計入，達上限丟 LLMError。
  - 送出前估算 token（estimate_tokens）；超過 LLM_MAX_PROMPT_TOKENS（預設 200K）丟 LLMError
    （組裝器會先自行縮減候選池與新聞摘要，這裡是最後一道防線）。
  - model 屬性 = 最近一次成功回應的實際模型，供 agent_metadata.model_version。
RPD 依 Google 的重置時間（太平洋時間午夜）計算。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import ROOT
from .llm import CONN_STATUS, TIMEOUT_STATUS, LLMError, LLMHTTPError, LLMResult

USAGE_PATH = ROOT / "runs" / "state" / "llm_usage.json"
PT, TPE = ZoneInfo("America/Los_Angeles"), ZoneInfo("Asia/Taipei")


def estimate_tokens(text: str) -> int:
    """保守估計：CJK 字元每字 1 token，其他字元每 4 字 1 token。"""
    cjk = len(re.findall(r"[　-鿿＀-￯]", text))
    return cjk + (len(text) - cjk + 3) // 4


DOTENV_PATH = ROOT / ".env"           # 測試會把它指到不存在的路徑，避免讀到本機真實 .env


def load_dotenv(path: Path | None = None) -> list[str]:
    """讀 .env（KEY=VALUE；# 註解）；不覆寫已存在的環境變數。回傳設定的鍵名。"""
    p = path or DOTENV_PATH
    out = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
            out.append(k)
    return out


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class QuotaClient:
    def __init__(self, primary, fallback=None, *, usage_path: Path | None = None, min_interval: float | None = None,
                 daily_limit: int | None = None, primary_rpd: int | None = None, max_prompt_tokens: int | None = None,
                 max_tries: int = 4, max_consecutive_failures: int = 3, sleep=time.sleep, clock=time.time):
        self.primary, self.fallback = primary, fallback
        self.provider = getattr(primary, "provider", "other")
        self.usage_path = usage_path or USAGE_PATH
        self.min_interval = _env_num("LLM_MIN_INTERVAL", 13) if min_interval is None else min_interval
        self.daily_limit = int(_env_num("LLM_DAILY_CALL_LIMIT", 10) if daily_limit is None else daily_limit)
        self.primary_rpd = int(_env_num("LLM_PRIMARY_RPD", 20) if primary_rpd is None else primary_rpd)
        self.max_prompt_tokens = int(_env_num("LLM_MAX_PROMPT_TOKENS", 200_000) if max_prompt_tokens is None
                                     else max_prompt_tokens)
        self.max_tries, self.max_fail = max_tries, max_consecutive_failures
        self.sleep, self.clock = sleep, clock
        self.use_fallback = False
        self.consecutive_failures = 0
        self.model = primary.model
        self.log: list[dict] = []                     # 每次 HTTP 呼叫的紀錄（存進 runs/<T>/raw/）

    # ---- 使用量
    def _load(self) -> dict:
        if self.usage_path.exists():
            try:
                return json.loads(self.usage_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {"last_call": 0.0, "calls_by_day": {}, "rpd": {}}

    def _save(self, u: dict) -> None:
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        self.usage_path.write_text(json.dumps(u, ensure_ascii=False, indent=1), encoding="utf-8")

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), TPE).date().isoformat()

    def _pt_day(self) -> str:
        return datetime.fromtimestamp(self.clock(), PT).date().isoformat()

    def calls_today(self) -> int:
        return int(self._load()["calls_by_day"].get(self._today(), 0))

    def _rpd_used(self, model: str) -> int:
        return int(self._load()["rpd"].get(self._pt_day(), {}).get(model, 0))

    def _record(self, model: str) -> None:
        u = self._load()
        u["last_call"] = self.clock()
        u["calls_by_day"][self._today()] = int(u["calls_by_day"].get(self._today(), 0)) + 1
        day = u["rpd"].setdefault(self._pt_day(), {})
        day[model] = int(day.get(model, 0)) + 1
        u["rpd"] = {k: v for k, v in u["rpd"].items() if k >= self._pt_day()}
        self._save(u)

    # ---- 呼叫
    def _client(self):
        if not self.use_fallback and self.primary_rpd and self._rpd_used(self.primary.model) >= self.primary_rpd:
            self._switch(f"主模型 {self.primary.model} 今日已用 {self.primary_rpd} 次（RPD 上限）")
        return self.fallback if self.use_fallback and self.fallback is not None else self.primary

    def _switch(self, why: str) -> None:
        if not self.use_fallback and self.fallback is not None:
            self.use_fallback = True
            self.log.append({"event": "switch_to_fallback", "reason": why, "model": self.fallback.model})

    def _wait_interval(self) -> None:
        wait = self._load()["last_call"] + self.min_interval - self.clock()
        if wait > 0:
            self.sleep(wait)

    def generate(self, system: str, user: str, **kw) -> LLMResult:
        est = estimate_tokens(system) + estimate_tokens(user)
        if est > self.max_prompt_tokens:
            raise LLMError(f"提示詞估計 {est:,} tokens，超過上限 {self.max_prompt_tokens:,}（請縮減候選池或新聞）")
        last: Exception | None = None
        for attempt in range(self.max_tries):
            if self.calls_today() >= self.daily_limit:
                raise LLMError(f"今日 LLM 呼叫已達上限 {self.daily_limit} 次")
            client = self._client()
            self._wait_interval()
            self._record(client.model)
            t0 = self.clock()
            try:
                res = client.generate(system, user, **kw)
            except LLMError as e:
                last = e
                status = getattr(e, "status", 0)
                body = getattr(e, "body", "") or str(e)
                self.log.append({"event": "error", "model": client.model, "status": status, "error": str(e)[:300],
                                 "est_tokens": est})
                if client is self.primary and not self.use_fallback:
                    self.consecutive_failures += 1
                    if status == 429 and re.search(r"per.?day|PerDay|daily", body, re.I):
                        self._switch("主模型當日額度用完（429 per-day）")
                    elif status == TIMEOUT_STATUS:
                        self._switch(f"主模型 {client.model} 逾時，直接改用備援（不再重試主模型）")
                    elif status == 404:
                        self._switch(f"主模型 {client.model} 不存在（404，請確認 AI Studio 的模型 ID）")
                    elif self.consecutive_failures >= self.max_fail:
                        self._switch(f"主模型連續失敗 {self.consecutive_failures} 次")
                retriable = status in (429, CONN_STATUS, TIMEOUT_STATUS) or status >= 500
                if not retriable and not (client is self.primary and self.use_fallback):
                    raise
                if attempt + 1 < self.max_tries and not (self.use_fallback and client is self.primary):
                    self.sleep(min(15 * 2 ** attempt, 120))
                continue
            if client is self.primary:
                self.consecutive_failures = 0
            self.model = res.model or client.model
            self.log.append({"event": "ok", "model": self.model, "input_tokens": res.input_tokens,
                             "output_tokens": res.output_tokens, "est_tokens": est,
                             "seconds": round(self.clock() - t0, 2)})
            return res
        raise LLMError(f"LLM 呼叫失敗（{self.max_tries} 次）：{last}")
