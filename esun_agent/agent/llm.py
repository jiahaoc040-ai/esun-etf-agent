"""LLM 供應商（可切換）與輸出解析。

LLM 只輸出兩件事（JSON）：
  (a) market_view：regime、stance、logic、basis_refs（市場級 obs_id）、counter_evidence、confidence
  (b) tilts：對個股的加減碼變更 [{ticker, delta, basis, event_direction, obs_ids, logic, counter_evidence, confidence}]
      delta 是這檔 tilt 的「新總量」（占 NAV，+0.02 = 加碼 2%）；0 = 撤銷；沒提到的檔沿用現有 tilt。
LLM 不得決定權重或股數：輸出裡出現 weight／shares／orders 等欄位一律視為錯誤，由程式（base_weights → apply_tilts →
derive_orders）機械產生。posture 與現金區間由實際委託推得，不由 LLM 填。

供應商：預設 Gemini（環境變數 GOOGLE_API_KEY），另支援 Anthropic（ANTHROPIC_API_KEY）。以 LLM_PROVIDER=gemini|anthropic
或 make_client(provider=...) 切換；模型以 LLM_MODEL 覆寫。用 requests 直連 REST，不依賴各家 SDK。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

import requests

DEFAULT_MODELS = {"gemini": "gemini-2.5-pro", "anthropic": "claude-sonnet-5-5"}
PROVIDER_NAME = {"gemini": "google", "anthropic": "anthropic"}          # agent_metadata.model_provider
FORBIDDEN_KEYS = {"weight", "weights", "target_weight", "shares", "quantity", "orders", "order", "price", "amount",
                  "posture", "target_cash_pct_range", "net_exposure_intent"}
REGIMES = {"risk_on", "neutral", "risk_off"}
STANCES = {"aggressive", "neutral", "defensive"}
BASES = {"financial_report", "investor_conference", "monthly_revenue", "trust_flow", "material_news"}


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLMError(RuntimeError):
    pass


class GeminiClient:
    provider = "google"

    def __init__(self, api_key: str | None = None, model: str | None = None, session=None, timeout: float = 120):
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not self.api_key:
            raise LLMError("缺少 GOOGLE_API_KEY")
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODELS["gemini"]
        self.session = session or requests.Session()
        self.timeout = timeout

    def generate(self, system: str, user: str, *, temperature: float = 0.2) -> LLMResult:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"}}
        r = self.session.post(url, params={"key": self.api_key}, json=body, timeout=self.timeout)
        if r.status_code != 200:
            raise LLMError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
        d = r.json()
        try:
            text = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Gemini 回應格式不符: {str(d)[:300]}") from e
        u = d.get("usageMetadata", {})
        return LLMResult(text, int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0)))


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None, session=None, timeout: float = 120,
                 max_tokens: int = 4096):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise LLMError("缺少 ANTHROPIC_API_KEY")
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODELS["anthropic"]
        self.session = session or requests.Session()
        self.timeout, self.max_tokens = timeout, max_tokens

    def generate(self, system: str, user: str, *, temperature: float = 0.2) -> LLMResult:
        body = {"model": self.model, "max_tokens": self.max_tokens, "temperature": temperature, "system": system,
                "messages": [{"role": "user", "content": user}]}
        r = self.session.post("https://api.anthropic.com/v1/messages", json=body, timeout=self.timeout,
                              headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"})
        if r.status_code != 200:
            raise LLMError(f"Anthropic HTTP {r.status_code}: {r.text[:300]}")
        d = r.json()
        try:
            text = "".join(b.get("text", "") for b in d["content"] if b.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise LLMError(f"Anthropic 回應格式不符: {str(d)[:300]}") from e
        u = d.get("usage", {})
        return LLMResult(text, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)))


def make_client(provider: str | None = None, **kw):
    """provider：'gemini'（預設）或 'anthropic'；未指定時讀環境變數 LLM_PROVIDER。"""
    provider = (provider or os.environ.get("LLM_PROVIDER") or "gemini").lower()
    if provider == "gemini":
        return GeminiClient(**kw)
    if provider == "anthropic":
        return AnthropicClient(**kw)
    raise LLMError(f"不支援的供應商 {provider!r}（gemini | anthropic）")


# ----------------------------------------------------------------------------- 輸出解析

@dataclass
class MarketViewIn:
    regime: str
    stance: str
    logic: str
    basis_refs: list[str]
    counter_evidence: str | None
    confidence: float | None = None


@dataclass
class TiltChange:
    ticker: str
    delta: float
    basis: str | None
    event_direction: str | None
    obs_ids: list[str]
    logic: str
    counter_evidence: str | None
    confidence: float | None = None


@dataclass
class Proposal:
    market_view: MarketViewIn
    tilts: list[TiltChange] = field(default_factory=list)


def extract_json(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise


def _forbidden(obj, path="") -> list[str]:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                found.append(f"{path}/{k}")
            found += _forbidden(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += _forbidden(v, f"{path}[{i}]")
    return found


def parse_proposal(text: str) -> tuple[Proposal | None, list[str]]:
    """解析並檢查 LLM 輸出的結構與型別。回傳 (Proposal 或 None, 錯誤清單)。語意檢查（引用、持有規則）在 assemble 做。"""
    try:
        d = extract_json(text)
    except (json.JSONDecodeError, ValueError):
        return None, ["輸出不是合法 JSON，請只輸出一個 JSON 物件"]
    if not isinstance(d, dict):
        return None, ["輸出頂層必須是 JSON 物件"]
    errs = [f"不得輸出 {p}：權重、股數、委託與 posture 由程式決定" for p in _forbidden(d)]
    extra = set(d) - {"market_view", "tilts"}
    if extra:
        errs.append(f"頂層只允許 market_view 與 tilts，多出 {sorted(extra)}")
    mv = d.get("market_view")
    if not isinstance(mv, dict):
        return None, errs + ["缺少 market_view 物件"]
    bad = set(mv) - {"regime", "stance", "logic", "basis_refs", "counter_evidence", "confidence"}
    if bad:
        errs.append(f"market_view 多出欄位 {sorted(bad)}")
    if mv.get("regime") not in REGIMES:
        errs.append(f"market_view.regime 需為 {sorted(REGIMES)}")
    if mv.get("stance") not in STANCES:
        errs.append(f"market_view.stance 需為 {sorted(STANCES)}")
    if not isinstance(mv.get("logic"), str) or not 20 <= len(mv["logic"]) <= 1000:
        errs.append("market_view.logic 需為 20–1000 字的字串")
    refs = mv.get("basis_refs")
    if not (isinstance(refs, list) and 1 <= len(refs) <= 10 and all(isinstance(r, str) and re.fullmatch(r"O\d{1,3}", r) for r in refs)):
        errs.append("market_view.basis_refs 需為 1–10 個 obs_id（例如 [\"O1\",\"O3\"]）")
    ce = mv.get("counter_evidence")
    if ce is not None and not (isinstance(ce, str) and len(ce) <= 500):
        errs.append("market_view.counter_evidence 需為 ≤500 字的字串或 null")
    conf = mv.get("confidence")
    if conf is not None and not (isinstance(conf, (int, float)) and 0 <= conf <= 1):
        errs.append("market_view.confidence 需在 0–1")

    tilts_in = d.get("tilts", [])
    if not isinstance(tilts_in, list):
        return None, errs + ["tilts 必須是陣列（沒有變更就給 []）"]
    tilts, seen = [], set()
    for i, t in enumerate(tilts_in):
        w = f"tilts[{i}]"
        if not isinstance(t, dict):
            errs.append(f"{w} 必須是物件")
            continue
        bad = set(t) - {"ticker", "delta", "basis", "event_direction", "obs_ids", "logic", "counter_evidence", "confidence"}
        if bad:
            errs.append(f"{w} 多出欄位 {sorted(bad)}")
        tk = t.get("ticker")
        if not (isinstance(tk, str) and re.fullmatch(r"\d{4}", tk)):
            errs.append(f"{w}.ticker 需為 4 位數字字串")
        elif tk in seen:
            errs.append(f"{w}.ticker {tk} 重複")
        seen.add(tk)
        dl = t.get("delta")
        if isinstance(dl, bool) or not isinstance(dl, (int, float)):
            errs.append(f"{w}.delta 需為數字（占 NAV 的比例，例如 0.02）")
            continue
        if t.get("basis") not in BASES:
            errs.append(f"{w}.basis 需為 {sorted(BASES)}")
        if t.get("event_direction") not in ("positive", "negative"):
            errs.append(f"{w}.event_direction 需為 positive 或 negative（所引新事件對該股的方向）")
        ids = t.get("obs_ids")
        if not (isinstance(ids, list) and 1 <= len(ids) <= 10 and all(isinstance(r, str) and re.fullmatch(r"O\d{1,3}", r) for r in ids)):
            errs.append(f"{w}.obs_ids 需為 1–10 個 obs_id")
        if not isinstance(t.get("logic"), str) or not 20 <= len(t["logic"]) <= 1000:
            errs.append(f"{w}.logic 需為 20–1000 字的字串")
        ce2 = t.get("counter_evidence")
        if ce2 is not None and not (isinstance(ce2, str) and len(ce2) <= 500):
            errs.append(f"{w}.counter_evidence 需為 ≤500 字的字串或 null")
        c2 = t.get("confidence")
        if c2 is not None and not (isinstance(c2, (int, float)) and 0 <= c2 <= 1):
            errs.append(f"{w}.confidence 需在 0–1")
        tilts.append(TiltChange(ticker=str(tk), delta=float(dl), basis=t.get("basis"),
                                event_direction=t.get("event_direction"), obs_ids=list(ids) if isinstance(ids, list) else [],
                                logic=str(t.get("logic", "")), counter_evidence=ce2, confidence=c2))
    if errs:
        return None, errs
    return Proposal(MarketViewIn(mv["regime"], mv["stance"], mv["logic"], mv["basis_refs"], mv.get("counter_evidence"),
                                 mv.get("confidence")), tilts), []
