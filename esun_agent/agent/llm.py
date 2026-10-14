"""LLM 供應商（可切換）與輸出解析。

集中策略（正式版，parse_picks）LLM 輸出 market_view、picks（10–12 檔高信心名單：ticker、conviction=high|medium、basis、
obs_ids、logic…）與 exits（主動移出者的理由）；權重由程式依信心等級換算（高 9%、中 6%）。
舊版（降級保底沿用的合規基準 A ＋ tilt，parse_proposal）LLM 只輸出兩件事（JSON）：
  (a) market_view：regime、stance、logic、basis_refs（市場級 obs_id）、counter_evidence、confidence
  (b) tilts：對個股的加減碼變更 [{ticker, delta, basis, event_direction, obs_ids, logic, counter_evidence, confidence}]
      delta 是這檔 tilt 的「新總量」（占 NAV，+0.02 = 加碼 2%）；0 = 撤銷；沒提到的檔沿用現有 tilt。
LLM 不得決定權重或股數：輸出裡出現 weight／shares／orders 等欄位一律視為錯誤，由程式（base_weights → apply_tilts →
derive_orders）機械產生。posture 與現金區間由實際委託推得，不由 LLM 填。

供應商：預設 Google Gemini 免費方案（GOOGLE_API_KEY；主模型 LLM_MODEL、備援 LLM_FALLBACK_MODEL），另支援 Anthropic
（LLM_PROVIDER=anthropic、ANTHROPIC_API_KEY、ANTHROPIC_MODEL）作付費備援。make_client() 回傳 llm_budget.QuotaClient：
呼叫間隔、429／5xx 退避、主模型失敗改備援、單日呼叫上限、token 估算（見 llm_budget.py）。用 requests 直連 REST。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

import requests

DEFAULT_MODELS = {"gemini": "gemini-3.8-flash", "gemini_fallback": "gemini-3.5-flash-lite", "anthropic": "claude-sonnet-5-5"}
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
    model: str | None = None                  # 實際回應的模型（agent_metadata.model_version）


class LLMError(RuntimeError):
    pass


TIMEOUT_STATUS = 598                          # 讀取／連線逾時（虛擬狀態碼）
CONN_STATUS = 599                             # 其他連線錯誤
GEMINI_TIMEOUT = 60.0                         # 秒


class LLMHTTPError(LLMError):
    """HTTP 非 200。status 供重試判斷：429（額度）、5xx（暫時性）、598（逾時）、599（連線錯誤）。"""

    def __init__(self, msg: str, status: int, body: str = ""):
        super().__init__(msg)
        self.status, self.body = status, body


class GeminiClient:
    provider = "google"

    def __init__(self, api_key: str | None = None, model: str | None = None, session=None,
                 timeout: float = GEMINI_TIMEOUT):
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
        try:
            r = self.session.post(url, params={"key": self.api_key}, json=body, timeout=self.timeout)
        except requests.Timeout as e:
            raise LLMHTTPError(f"Gemini 逾時（{self.timeout:.0f} 秒）：{self.model}", TIMEOUT_STATUS) from e
        except requests.RequestException as e:
            raise LLMHTTPError(f"Gemini 連線失敗：{e}", CONN_STATUS) from e
        if r.status_code != 200:
            raise LLMHTTPError(f"Gemini HTTP {r.status_code}: {r.text[:300]}", r.status_code, r.text[:2000])
        d = r.json()
        try:
            text = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Gemini 回應格式不符: {str(d)[:300]}") from e
        u = d.get("usageMetadata", {})
        return LLMResult(text, int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0)),
                         d.get("modelVersion") or self.model)


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None, session=None, timeout: float = 120,
                 max_tokens: int = 4096):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise LLMError("缺少 ANTHROPIC_API_KEY")
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODELS["anthropic"]
        self.session = session or requests.Session()
        self.timeout, self.max_tokens = timeout, max_tokens

    def generate(self, system: str, user: str, *, temperature: float = 0.2) -> LLMResult:
        body = {"model": self.model, "max_tokens": self.max_tokens, "temperature": temperature, "system": system,
                "messages": [{"role": "user", "content": user}]}
        try:
            r = self.session.post("https://api.anthropic.com/v1/messages", json=body, timeout=self.timeout,
                                  headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                                           "content-type": "application/json"})
        except requests.RequestException as e:
            raise LLMHTTPError(f"Anthropic 連線失敗：{e}", 599) from e
        if r.status_code != 200:
            raise LLMHTTPError(f"Anthropic HTTP {r.status_code}: {r.text[:300]}", r.status_code, r.text[:2000])
        d = r.json()
        try:
            text = "".join(b.get("text", "") for b in d["content"] if b.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise LLMError(f"Anthropic 回應格式不符: {str(d)[:300]}") from e
        u = d.get("usage", {})
        return LLMResult(text, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)), d.get("model") or self.model)


def make_client(provider: str | None = None, *, managed: bool = True, **kw):
    """provider：'gemini'（預設）或 'anthropic'；未指定時讀環境變數 LLM_PROVIDER。
    managed=True（預設）回傳 QuotaClient（Gemini：主模型 LLM_MODEL＋備援 LLM_FALLBACK_MODEL）；False 回傳單一 client。"""
    provider = (provider or os.environ.get("LLM_PROVIDER") or "gemini").lower()
    if provider == "gemini":
        primary = GeminiClient(model=kw.pop("model", None) or os.environ.get("LLM_MODEL") or DEFAULT_MODELS["gemini"], **kw)
        if not managed:
            return primary
        from .llm_budget import QuotaClient
        fb_model = os.environ.get("LLM_FALLBACK_MODEL") or DEFAULT_MODELS["gemini_fallback"]
        fallback = GeminiClient(api_key=primary.api_key, model=fb_model, session=primary.session, timeout=primary.timeout)
        return QuotaClient(primary, fallback)
    if provider == "anthropic":
        c = AnthropicClient(**kw)
        if not managed:
            return c
        from .llm_budget import QuotaClient
        return QuotaClient(c, None, min_interval=0.0)
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


# ----------------------------------------------------------------------------- 集中策略：高信心名單

CONVICTIONS = {"high", "medium"}
OBS_RE = re.compile(r"O\d{1,3}")


@dataclass
class PickIn:
    ticker: str
    conviction: str
    basis: str
    obs_ids: list[str]
    logic: str
    counter_evidence: str | None
    confidence: float | None = None


@dataclass
class ExitIn:
    ticker: str
    obs_ids: list[str]
    logic: str


@dataclass
class PicksProposal:
    market_view: MarketViewIn
    picks: list[PickIn] = field(default_factory=list)
    exits: list[ExitIn] = field(default_factory=list)

    def levels(self) -> list[tuple[str, str]]:
        return [(p.ticker, p.conviction) for p in self.picks]


def _market_view(mv, errs: list[str]) -> MarketViewIn | None:
    if not isinstance(mv, dict):
        errs.append("缺少 market_view 物件")
        return None
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
    if not (isinstance(refs, list) and 1 <= len(refs) <= 10 and all(isinstance(r, str) and OBS_RE.fullmatch(r) for r in refs)):
        errs.append("market_view.basis_refs 需為 1–10 個 obs_id（例如 [\"O1\",\"O3\"]）")
    ce = mv.get("counter_evidence")
    if ce is not None and not (isinstance(ce, str) and len(ce) <= 500):
        errs.append("market_view.counter_evidence 需為 ≤500 字的字串或 null")
    conf = mv.get("confidence")
    if conf is not None and not (isinstance(conf, (int, float)) and not isinstance(conf, bool) and 0 <= conf <= 1):
        errs.append("market_view.confidence 需在 0–1")
    return MarketViewIn(mv.get("regime"), mv.get("stance"), mv.get("logic"), refs if isinstance(refs, list) else [],
                        ce, conf)


def _obs_list(x) -> bool:
    return isinstance(x, list) and 1 <= len(x) <= 10 and all(isinstance(r, str) and OBS_RE.fullmatch(r) for r in x)


def parse_picks(text: str) -> tuple[PicksProposal | None, list[str]]:
    """解析集中策略的 LLM 輸出（結構與型別）。名單規則（檔數、持有天數、換股上限、停損、事件前一日）與引用檢查在 assemble 做。"""
    try:
        d = extract_json(text)
    except (json.JSONDecodeError, ValueError):
        return None, ["輸出不是合法 JSON，請只輸出一個 JSON 物件"]
    if not isinstance(d, dict):
        return None, ["輸出頂層必須是 JSON 物件"]
    errs = [f"不得輸出 {p}：權重、股數、委託與 posture 由程式決定" for p in _forbidden(d)]
    extra = set(d) - {"market_view", "picks", "exits"}
    if extra:
        errs.append(f"頂層只允許 market_view、picks、exits，多出 {sorted(extra)}")
    mv = _market_view(d.get("market_view"), errs)
    picks_in, exits_in = d.get("picks"), d.get("exits", [])
    if not isinstance(picks_in, list):
        return None, errs + ["picks 必須是陣列（10–12 檔高信心名單）"]
    if not isinstance(exits_in, list):
        return None, errs + ["exits 必須是陣列（沒有主動移出就給 []）"]
    picks, seen = [], set()
    for i, t in enumerate(picks_in):
        w = f"picks[{i}]"
        if not isinstance(t, dict):
            errs.append(f"{w} 必須是物件")
            continue
        bad = set(t) - {"ticker", "conviction", "basis", "obs_ids", "logic", "counter_evidence", "confidence"}
        if bad:
            errs.append(f"{w} 多出欄位 {sorted(bad)}")
        tk = t.get("ticker")
        if not (isinstance(tk, str) and re.fullmatch(r"\d{4}", tk)):
            errs.append(f"{w}.ticker 需為 4 位數字字串")
        elif tk in seen:
            errs.append(f"{w}.ticker {tk} 重複")
        seen.add(tk)
        if t.get("conviction") not in CONVICTIONS:
            errs.append(f"{w}.conviction 需為 high（高）或 medium（中）")
        if t.get("basis") not in BASES:
            errs.append(f"{w}.basis 需為 {sorted(BASES)}")
        if not _obs_list(t.get("obs_ids")):
            errs.append(f"{w}.obs_ids 需為 1–10 個 obs_id")
        if not isinstance(t.get("logic"), str) or not 20 <= len(t["logic"]) <= 1000:
            errs.append(f"{w}.logic 需為 20–1000 字的字串")
        ce = t.get("counter_evidence")
        if ce is not None and not (isinstance(ce, str) and len(ce) <= 500):
            errs.append(f"{w}.counter_evidence 需為 ≤500 字的字串或 null")
        c = t.get("confidence")
        if c is not None and not (isinstance(c, (int, float)) and not isinstance(c, bool) and 0 <= c <= 1):
            errs.append(f"{w}.confidence 需在 0–1")
        picks.append(PickIn(str(tk), t.get("conviction"), t.get("basis"),
                            list(t["obs_ids"]) if isinstance(t.get("obs_ids"), list) else [], str(t.get("logic", "")), ce, c))
    exits, seen_x = [], set()
    for i, t in enumerate(exits_in):
        w = f"exits[{i}]"
        if not isinstance(t, dict):
            errs.append(f"{w} 必須是物件")
            continue
        bad = set(t) - {"ticker", "obs_ids", "logic"}
        if bad:
            errs.append(f"{w} 多出欄位 {sorted(bad)}")
        tk = t.get("ticker")
        if not (isinstance(tk, str) and re.fullmatch(r"\d{4}", tk)) or tk in seen_x:
            errs.append(f"{w}.ticker 需為不重複的 4 位數字字串")
        seen_x.add(tk)
        if tk in seen:
            errs.append(f"{w}.ticker {tk} 同時出現在 picks 與 exits")
        if not _obs_list(t.get("obs_ids")):
            errs.append(f"{w}.obs_ids 需為 1–10 個 obs_id")
        if not isinstance(t.get("logic"), str) or not 20 <= len(t["logic"]) <= 1000:
            errs.append(f"{w}.logic 需為 20–1000 字的字串")
        exits.append(ExitIn(str(tk), list(t["obs_ids"]) if isinstance(t.get("obs_ids"), list) else [], str(t.get("logic", ""))))
    if errs:
        return None, errs
    return PicksProposal(mv, picks, exits), []
