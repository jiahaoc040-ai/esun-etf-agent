"""QuotaClient：間隔、退避、主模型失敗改備援、單日上限、RPD、token 估算；make_client 與 .env；組裝器縮減提示詞。"""
import json

import pytest

from esun_agent.agent import llm as L
from esun_agent.agent import llm_budget as B


class Script:
    """依序回傳結果或丟例外的假 client。"""
    provider = "google"

    def __init__(self, model, outcomes):
        self.model, self.outcomes, self.calls = model, list(outcomes), 0

    def generate(self, system, user, **kw):
        self.calls += 1
        o = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(o, Exception):
            raise o
        return L.LLMResult(f'{{"by": "{self.model}"}}', 100, 10, f"{self.model}-001")


class Clock:
    def __init__(self, t=1_792_000_000.0):          # 2026-10 某日
        self.t, self.sleeps = t, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s


def qc(tmp_path, primary, fallback=None, **kw):
    c = Clock()
    q = B.QuotaClient(primary, fallback, usage_path=tmp_path / "u.json", sleep=c.sleep, clock=c,
                      **{"min_interval": 13, "daily_limit": 10, "primary_rpd": 20, **kw})
    return q, c


def err(status, body=""):
    return L.LLMHTTPError(f"HTTP {status}", status, body)


def test_interval_between_calls_and_model_recorded(tmp_path):
    p = Script("gemini-3.8-flash", ["ok", "ok"])
    q, c = qc(tmp_path, p)
    r1 = q.generate("s", "u")
    c.t += 2                                                      # 2 秒後再呼叫 → 要等 11 秒
    q.generate("s", "u")
    assert c.sleeps == [11.0] and r1.model == "gemini-3.8-flash-001" and q.model == "gemini-3.8-flash-001"
    assert q.calls_today() == 2 and [e["event"] for e in q.log] == ["ok", "ok"]
    u = json.loads((tmp_path / "u.json").read_text())
    assert list(u["rpd"].values())[0] == {"gemini-3.8-flash": 2}


def test_backoff_on_429_and_5xx_then_success(tmp_path):
    p = Script("m", [err(429, "rate limit per minute"), err(503), "ok"])
    q, c = qc(tmp_path, p)
    r = q.generate("s", "u")
    assert r.text and p.calls == 3 and not q.use_fallback
    assert c.sleeps == [15, 30]                                   # 退避 15、30 秒（已超過 13 秒間隔，不必再等）
    assert q.calls_today() == 3


def test_primary_three_failures_switch_to_fallback(tmp_path):
    p, f = Script("main", [err(500), err(500), err(500)]), Script("lite", ["ok"])
    q, _ = qc(tmp_path, p, f)
    r = q.generate("s", "u")
    assert p.calls == 3 and f.calls == 1 and q.use_fallback and r.model == "lite-001" and q.model == "lite-001"
    assert any(e.get("event") == "switch_to_fallback" for e in q.log)
    q.generate("s", "u")                                          # 之後都用備援
    assert p.calls == 3 and f.calls == 2


@pytest.mark.parametrize("e", [err(429, '{"error": {"status": "RESOURCE_EXHAUSTED", "details": "GenerateRequestsPerDayPerProjectPerModel"}}'),
                               err(404, "models/gemini-9 is not found")])
def test_daily_quota_or_bad_model_switches_immediately(tmp_path, e):
    p, f = Script("main", [e]), Script("lite", ["ok"])
    q, c = qc(tmp_path, p, f)
    assert q.generate("s", "u").model == "lite-001" and p.calls == 1
    assert 15 not in c.sleeps                                     # 切換時不退避


def test_daily_call_limit_and_rpd(tmp_path):
    p, f = Script("main", []), Script("lite", [])
    q, c = qc(tmp_path, p, f, daily_limit=3, primary_rpd=2)
    q.generate("s", "u")
    q.generate("s", "u")
    assert p.calls == 2
    q.generate("s", "u")                                          # 主模型 RPD 用完 → 備援
    assert f.calls == 1 and q.use_fallback
    with pytest.raises(L.LLMError, match="上限 3 次"):
        q.generate("s", "u")


def test_non_retriable_error_raises_and_token_guard(tmp_path):
    q, _ = qc(tmp_path, Script("m", [err(400, "bad request")]))
    with pytest.raises(L.LLMError, match="400"):
        q.generate("s", "u")
    q2, _ = qc(tmp_path, Script("m", []), max_prompt_tokens=100)
    with pytest.raises(L.LLMError, match="超過上限"):
        q2.generate("系" * 90, "u" * 80)
    assert B.estimate_tokens("台積電" + "abcd" * 10) == 13


def test_make_client_models_and_dotenv(tmp_path, monkeypatch):
    for k in ("LLM_MODEL", "LLM_FALLBACK_MODEL", "LLM_PROVIDER", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "XYZ"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("# 註解\nGOOGLE_API_KEY=g-key\nexport LLM_FALLBACK_MODEL='gemini-lite-x'\nXYZ=\"1\"\n", encoding="utf-8")
    monkeypatch.setenv("XYZ", "keep")
    assert sorted(B.load_dotenv(env)) == ["GOOGLE_API_KEY", "LLM_FALLBACK_MODEL"]
    import os
    assert os.environ["XYZ"] == "keep"                            # 不覆寫既有變數
    c = L.make_client()
    assert isinstance(c, B.QuotaClient) and c.provider == "google"
    assert c.primary.model == "gemini-3.8-flash" and c.fallback.model == "gemini-lite-x"
    monkeypatch.setenv("LLM_MODEL", "gemini-x")
    assert L.make_client().primary.model == "gemini-x" and L.make_client(managed=False).model == "gemini-x"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    a = L.make_client("anthropic")
    assert a.provider == "anthropic" and a.fallback is None and a.min_interval == 0 and a.model.startswith("claude")


def test_gemini_reports_model_version():
    class Resp:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "{}"}]}}], "modelVersion": "gemini-3.8-flash-002",
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}

    class Sess:
        def post(self, *a, **k):
            return Resp()
    assert L.GeminiClient(api_key="k", model="gemini-3.8-flash", session=Sess()).generate("s", "u").model == "gemini-3.8-flash-002"


def test_agent_shrinks_prompt_and_records_actual_model(tmp_path):
    from esun_agent.data.etf_holdings import load_top15
    from esun_agent.data.prices import load_panel
    from esun_agent.strategy.factors import compute_factors
    from esun_agent.strategy.marketcap import market_cap

    from .agent_helpers import ConvSim, FakeLLM, conviction_llm
    panel = load_panel()
    env = {"panel": panel, "factors": compute_factors(panel), "top15": load_top15(), "caps": market_cap(panel)[0]}

    class Versioned(FakeLLM):
        def generate(self, system, user, **kw):
            r = super().generate(system, user, **kw)
            r.model = "gemini-3.5-flash-lite-007"
            return r
    sim = ConvSim(env, tmp_path)
    full = FakeLLM([conviction_llm()])
    sim.day("2026-09-29", full)
    est_full = sim.results[-1].attempts[0]["est_tokens"]
    sim2 = ConvSim(env, tmp_path / "b")
    llm = Versioned([conviction_llm()])
    r = sim2.day("2026-09-29", llm, max_prompt_tokens=int(est_full * 0.8))
    a = r.attempts[0]
    assert a["shrink"] >= 1 and a["est_tokens"] < est_full and r.report.errors == []
    assert len([l for l in llm.calls[0][1].splitlines() if l.startswith("- 第 ")]) <= 21
    assert r.doc["agent_metadata"]["model_version"] == "gemini-3.5-flash-lite-007"


def test_smoke_test_script(tmp_path, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("smoke", Path(__file__).parent.parent / "scripts" / "llm_smoke_test.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    p, f = Script("gemini-3.8-flash", ["ok"]), Script("gemini-3.5-flash-lite", ["ok"])
    q, c = qc(tmp_path, p, f)
    monkeypatch.setattr(mod, "make_client", lambda provider=None: q)
    monkeypatch.setattr(mod, "load_dotenv", lambda: [])
    real = mod.QuotaClient
    monkeypatch.setattr(mod, "QuotaClient", lambda client, fb, **kw: real(client, fb, sleep=c.sleep, clock=c, **kw))
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "gemini-3.8-flash-001" in out and "gemini-3.5-flash-lite-001" in out and "tokens：輸入 100" in out
    assert p.calls == 1 and f.calls == 1 and c.sleeps == [13.0]
