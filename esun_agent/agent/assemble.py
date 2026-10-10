"""D-Plan 組裝器：base_weights → LLM tilts → apply_tilts(strict) → derive_orders → validate()。

流程（assemble_day）：
  1. 基準：base_plan（A 的權重，Active Share 補到 27%）。
  2. 證據：observations.build_evidence（行情、法人、夜盤、美股、事件、沿用中的舊事件）。
  3. LLM（最多 max_attempts=3 次呼叫）：只輸出 market_view 與 tilt 變更。任何一層不合格都把錯誤回饋 LLM 重試：
       解析 → 引用與證據（authority、新事件、最短持有）→ apply_tilts(strict=True) 的 TiltError
       → stance 與實際操作方向一致 → 組完整 D-Plan 後 validate() 的 ERROR。
  4. 三次都失敗 → 保底 D-Plan：零 tilt、只做基準必要調整（同樣要通過 validate，否則丟 AssembleError，絕不輸出不合格檔案）。
再平衡時點：距上次再平衡 ≥ 5 個交易日、tilt 與前一日不同、或漂移保護觸發（權重逼近上限／Active Share < 22%）才調整；
其餘日子所有持股寫入 no_trade_decisions（理由引用「權重未偏離無交易帶」的推論）。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..config import ROOT
from ..dplan_validate import Report, validate
from ..strategy.declaration import load_declaration
from ..strategy.factors import factors_on
from ..strategy.final import FinalStrategy, TiltError, apply_tilts, base_plan, validate_tilts
from ..universe import load_universe
from .llm import LLMError, Proposal, parse_proposal
from .observations import BASIS_LABELS, EvidenceBuilder, build_evidence, iso, now_taipei, prune
from .plan import Plan, assign_funding, plan_orders, posture_for
from .tilt_state import ActiveTilt, TiltState, check_change, trading_days_elapsed

PROMPT_DIR = ROOT / "prompts"
MAX_ATTEMPTS = 3
EVIDENCE_CUTOFF_TIME = "19:30:00+08:00"          # 決策日 D 的繳交起點：證據的 content_as_of 不得晚於此


class AssembleError(RuntimeError):
    """保底 D-Plan 也未通過驗證——不輸出任何檔案。"""


# ----------------------------------------------------------------------------- 版本

def prompt_version() -> str:
    h = hashlib.sha256()
    for name in ("system.md", "user.md"):
        h.update((PROMPT_DIR / name).read_bytes())
    return h.hexdigest()[:8]


def code_version() -> str:
    """git:<short sha>（工作目錄有未提交修改時加 -dirty）；環境變數 CODE_VERSION 可覆寫。"""
    if os.environ.get("CODE_VERSION"):
        return os.environ["CODE_VERSION"][:64]
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
        return f"git:{sha}" + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "git:unknown"


# ----------------------------------------------------------------------------- 輸入與輸出

@dataclass
class DayContext:
    team_id: str
    trade_date: str
    prev_date: str                                   # 決策日 D：最近一個已收盤的交易日
    panel: object
    factors: dict
    caps: object
    top10: dict | None
    prev_nav: float                                  # 主辦方結算的前日淨值
    cash: float
    holdings: dict[str, int]                         # 主辦方結算庫存
    inputs: dict = field(default_factory=dict)       # taifex / us / events
    strategy: FinalStrategy = field(default_factory=FinalStrategy)
    fetched_at: datetime | None = None
    declaration: dict | None = None

    @property
    def calendar(self) -> list[str]:
        return [d for d in self.panel.dates if d <= self.prev_date]

    @property
    def prev_close(self) -> dict[str, float]:
        return {t: float(v) for t, v in self.panel.close.loc[self.prev_date].dropna().items()}

    def validate_ctx(self) -> dict:
        return {"prev_nav": self.prev_nav, "cash": self.cash, "holdings": dict(self.holdings),
                "prev_close": self.prev_close}


@dataclass
class AssembleResult:
    doc: dict
    report: Report
    fallback: bool
    attempts: list[dict]
    state: TiltState
    rebalanced: bool
    usage: dict
    notes: list[str]

    @property
    def filename(self) -> str:
        return f"D-Plan_{self.doc['team_id']}_{self.doc['trade_date']}.json"


def write_dplan(result: AssembleResult, outdir: Path, state_path: Path | None = None) -> Path:
    """寫檔前再驗證一次（有 ERROR 就不寫）；寫檔後才更新 tilt 狀態。"""
    if not result.report.ok:
        raise AssembleError("D-Plan 有 ERROR，拒絕寫檔：\n" + str(result.report))
    outdir.mkdir(parents=True, exist_ok=True)
    p = outdir / result.filename
    p.write_text(json.dumps(result.doc, ensure_ascii=False, indent=2), encoding="utf-8")
    result.state.save(state_path)
    return p


# ----------------------------------------------------------------------------- prompt

def _declaration_summary(decl: dict) -> str:
    lim = decl["tilt"]["limits"]
    d = {"etf": decl["etf"]["name"], "theme": decl["theme"]["summary"], "philosophy": decl["philosophy"],
         "benchmark": decl["benchmark"]["definition"], "tilt_limits": lim,
         "allowed_bases": [{"id": b["id"], "label": b["label"], "authority": b["authority"]}
                           for b in decl["tilt"]["allowed_bases"]],
         "requirements": decl["tilt"]["requirements"]}
    return json.dumps(d, ensure_ascii=False, indent=1)


def _render(template: str, **kw) -> str:
    for k, v in kw.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def build_prompts(ctx: DayContext, decl: dict, ev, base: dict, state_tilts: dict[str, ActiveTilt], carried_ids: dict,
                  feedback: list[str]) -> tuple[str, str]:
    p = ctx.strategy.params
    bases = "\n".join(f"- {b['id']}（{b['label']}）：來源 authority 須為 {'／'.join(b['authority'])}"
                      for b in decl["tilt"]["allowed_bases"])
    system = _render((PROMPT_DIR / "system.md").read_text(encoding="utf-8"), etf_name=decl["etf"]["name"],
                     allowed_bases=bases, max_delta=f"{p.max_delta:.0%}", max_active=f"{p.max_active:.0%}",
                     min_hold_days=5)
    names = load_universe()
    base_txt = "、".join(f"{t} {names[t]['name']} {w:.1%}" for t, w in sorted(base.items(), key=lambda x: -x[1]))
    cal = ctx.calendar
    tl = []
    for t, a in sorted(state_tilts.items()):
        held = trading_days_elapsed(cal, a.since, ctx.trade_date)
        tl.append(f"- {t} {names.get(t, {}).get('name', '')}：{a.delta:+.2%}，自 {a.since} 起（已持有 {held} 個交易日，"
                  f"{'已滿' if held >= 5 else '未滿'}最短持有）；依據 {BASIS_LABELS.get(a.basis, a.basis)}，證據最新 {a.evidence_as_of}；"
                  f"證據 obs_id：{carried_ids.get(t, [])}")
    obs = []
    for o in ev.observations:
        m = ev.meta[o["obs_id"]]
        obs.append(json.dumps({"obs_id": o["obs_id"], "topic": o.get("topic"), "ticker": m.get("ticker"),
                               "authority": m.get("authorities"), "as_of": m["as_of"], "carried": m.get("carried"),
                               "statement": o["statement"], "values": o["values"]}, ensure_ascii=False))
    fb = ""
    if feedback:
        fb = "# 上一次輸出的問題（請修正後重新輸出完整 JSON）\n" + "\n".join(f"- {e}" for e in feedback)
    user = _render((PROMPT_DIR / "user.md").read_text(encoding="utf-8"), trade_date=ctx.trade_date,
                   prev_date=ctx.prev_date, prompt_version=prompt_version(),
                   declaration_summary=_declaration_summary(decl), base_summary=base_txt,
                   active_tilts="\n".join(tl) or "（目前沒有有效的 tilt）", observations="\n".join(obs), feedback=fb)
    return system, user


# ----------------------------------------------------------------------------- 提案檢查

def _carried(state: TiltState, base: dict, eligible: set, params, notes: list[str]) -> dict[str, ActiveTilt]:
    """狀態檔的 tilt 每天先過一次系統檢查：標的跌出可交易名單、或基準不再持有而 tilt 為負 → 系統撤銷（記 notes）。"""
    out = {}
    for t, a in state.tilts.items():
        errs = validate_tilts(base, {t: a.delta}, params, eligible)
        if errs:
            notes.append(f"system_revoked:{t}:{errs[0]}")
        else:
            out[t] = a
    return out


def check_proposal(prop: Proposal, ctx: DayContext, ev, carried: dict[str, ActiveTilt], decl: dict) -> list[str]:
    errs: list[str] = []
    ids = {o["obs_id"] for o in ev.observations}
    for r in prop.market_view.basis_refs:
        if r not in ids:
            errs.append(f"market_view.basis_refs 引用不存在的 {r}")
    cutoff = f"{ctx.prev_date}T{EVIDENCE_CUTOFF_TIME}"
    allowed = {b["id"]: set(b["authority"]) for b in decl["tilt"]["allowed_bases"]}
    uni = load_universe()
    for c in prop.tilts:
        w = f"tilt {c.ticker}"
        if c.ticker not in uni:
            errs.append(f"{w}：不在 150 檔名單")
            continue
        missing = [r for r in c.obs_ids if r not in ids]
        if missing:
            errs.append(f"{w}：obs_ids 引用不存在的 {missing}")
            continue
        cited = [o for o in c.obs_ids]
        good = [r for r in cited if ev.meta[r].get("basis") == c.basis and ev.meta[r].get("ticker") == c.ticker
                and set(ev.meta[r]["authorities"]) <= allowed[c.basis]]
        if not good:
            errs.append(f"{w}：obs_ids 中沒有任何一則是「{c.ticker} 的 {BASIS_LABELS[c.basis]}」事實"
                        f"（需 ticker={c.ticker}、basis={c.basis}、來源 authority ∈ {sorted(allowed[c.basis])}）")
            continue
        late = [r for r in good if ev.meta[r]["as_of"] > cutoff]
        if late:
            errs.append(f"{w}：{late} 的 content_as_of 晚於決策日 {cutoff}，不可引用")
        existing = carried.get(c.ticker)
        new_event = any((not ev.meta[r]["carried"]) and (existing is None or ev.meta[r]["as_of"] > existing.evidence_as_of)
                        for r in good)
        errs += check_change(existing, c.delta, c.event_direction, new_event, ctx.calendar, ctx.trade_date)
    return errs


# ----------------------------------------------------------------------------- D-Plan 組裝

def _label(t: str) -> str:
    return load_universe().get(t, {}).get("name", t)


def _tilt_notes(prop: Proposal | None, carried: dict[str, ActiveTilt], ev, merged: dict[str, float]) -> dict[str, dict]:
    """ticker → 該檔 tilt 的推論內容（今天新增／變更者用 LLM 的理由，沿用者用狀態檔的舊理由）。"""
    out: dict[str, dict] = {}
    changed = {c.ticker: c for c in (prop.tilts if prop else [])}
    for t, a in carried.items():
        if t in changed or t not in merged:
            continue
        out[t] = {"premises": list(ev.carried_ids.get(t, [])),
                  "logic": (f"沿用 {a.since} 起的{BASIS_LABELS.get(a.basis, a.basis)} tilt {a.delta:+.2%}：{a.logic.rstrip('。.')}"
                            f"；今日沒有新事件推翻，依最短持有與沿用規則維持。")[:1000],
                  "conclusion": f"{_label(t)} 維持 {a.delta:+.2%} 的 {BASIS_LABELS.get(a.basis, a.basis)} tilt"[:300],
                  "counter_evidence": a.counter_evidence, "confidence": a.confidence}
    for t, c in changed.items():
        verb = "撤銷" if abs(c.delta) < 1e-12 else ("加碼" if c.delta > 0 else "減碼")
        out[t] = {"premises": list(c.obs_ids), "logic": c.logic,
                  "conclusion": (f"{_label(t)} 依{BASIS_LABELS[c.basis]}{verb}"
                                 + ("" if verb == "撤銷" else f" {c.delta:+.2%}（相對基準權重）"))[:300],
                  "counter_evidence": c.counter_evidence, "confidence": c.confidence}
    return out


def build_doc(ctx: DayContext, ev, *, prop: Proposal | None, carried: dict[str, ActiveTilt], merged: dict[str, float],
              plan: Plan, due: bool, due_reason: str, usage: dict, started: datetime, completed: datetime,
              model_provider: str, model_version: str, with_funding: bool = True) -> dict:
    nav, close = ctx.prev_nav, ctx.prev_close
    D = ctx.prev_date
    b = EvidenceBuilder(ctx.fetched_at, ev)
    mkt = ev.by_topic("tw_market")[0]["obs_id"]
    inst = ev.by_topic("inst_flow")[0]["obs_id"]

    decisions = [{"decision_id": f"D{i}", **d} for i, d in enumerate(plan.decisions, 1)]
    cur = {t: n * close[t] / nav for t, n in ctx.holdings.items() if t in close}
    post = dict(ctx.holdings)
    for o in plan.orders:
        post[o.ticker] = post.get(o.ticker, 0) + (o.shares if o.side == "BUY" else -o.shares)
    n_post = sum(1 for n in post.values() if n > 0)
    posture = posture_for(plan, nav)
    if with_funding:
        assign_funding(decisions, plan, nav, close, posture["target_cash_pct_range"][0])

    # --- 系統 observation：權重偏離（依前日收盤價與前日 NAV）
    mk_src = [s["source_id"] for s in ev.sources if s["authority"] in ("twse", "tpex") and "日行情" in s.get("name", "")
              and s["content_as_of"].startswith(D)]
    dev_values = {"n_holdings": len(cur), "n_trades": len(decisions), "no_trade_band_pct": ctx.strategy.params.band * 100}
    traded_dev = [abs(d["target_weight"] - cur.get(d["ticker"], 0.0)) for d in decisions]
    dev_values["max_traded_dev_pct"] = max(traded_dev, default=0.0) * 100
    dev_obs = b.add_obs(mk_src[:5], "weights_vs_target",
                        f"依 {D} 收盤價與前日淨值計算：持股 {len(cur)} 檔，本日調整 {len(decisions)} 檔、"
                        f"維持 {len(plan.no_trade)} 檔；調整檔的權重變動最大 {dev_values['max_traded_dev_pct']:.2f}%，"
                        f"無交易帶 {dev_values['no_trade_band_pct']:.1f}%",
                        dev_values, as_of=f"{D}T13:30:00+08:00")

    # --- inferences
    notes = _tilt_notes(prop, carried, ev, merged)
    infs: list[dict] = []

    def add_inf(premises, logic, conclusion=None, ce=None, conf=None) -> str:
        infs.append({"inf_id": f"I{len(infs) + 1}", "premises": list(dict.fromkeys(premises)), "logic": logic[:1000],
                     "conclusion": (conclusion or "")[:300] or None, "counter_evidence": ce, "confidence": conf})
        return infs[-1]["inf_id"]

    touched = {d["ticker"] for d in decisions} | set(plan.no_trade)
    tilt_inf = {t: add_inf(n["premises"], n["logic"], n["conclusion"], n["counter_evidence"], n["confidence"])
                for t, n in sorted(notes.items()) if t in touched}
    band = ctx.strategy.params.band
    rebalance_inf = None
    if decisions:
        rebalance_inf = add_inf(
            [dev_obs, mkt],
            f"{due_reason}。依合規基準（市值加權、2330 ≤ 20%、個股 ≤ 8%、取前 28 檔、Active Share ≥ 27%）加上有效 tilt 重新計算目標權重，"
            f"與現權重偏離超過無交易帶 {band:.0%} 者才調整；委託由官方公式機械推導，與公開說明書宣告的核心＋事件加減碼框架一致。",
            f"調整 {len(decisions)} 檔以回到目標權重", None)
    notrade_inf = None
    if plan.no_trade:
        why = "今日不是再平衡日" if not due else f"{due_reason}，這些持股與目標權重差距在無交易帶內"
        notrade_inf = add_inf(
            [dev_obs],
            f"基準權重未偏離無交易帶：{why}；依無交易帶 {band:.0%} 規則維持原部位，不為微小偏離付出交易成本（往返約 0.6%）。",
            f"維持 {len(plan.no_trade)} 檔不動", None)

    for d in decisions:
        d["inference_refs"] = ([tilt_inf[d["ticker"]]] if d["ticker"] in tilt_inf else []) + [rebalance_inf]
        eff = cur.get(d["ticker"], 0.0)
        d["risk_check"] = (f"權重 {eff:.2%} → {d['target_weight']:.2%}（上限 {'25%' if d['ticker'] == '2330' else '10%'}）；"
                           f"委託後持股 {n_post} 檔 ∈ [20,30]")[:300]
    no_trade = []
    for t in plan.no_trade:
        refs = ([tilt_inf[t]] if t in tilt_inf else []) + [notrade_inf]
        no_trade.append({"ticker": t, "reason_refs": refs,
                         "reason": (f"權重 {cur.get(t, 0.0):.2%} 與目標差距在無交易帶 {band:.0%} 內" if due else
                                    f"非再平衡日，維持現有部位 {cur.get(t, 0.0):.2%}")[:300]})

    # --- market_view（regime／stance／理由由 LLM；posture 由實際委託推得）
    if prop is not None:
        mv_in = prop.market_view
        mv_refs = list(mv_in.basis_refs)
        mv = {"basis_refs": mv_refs, "logic": mv_in.logic, "regime": mv_in.regime, "stance": mv_in.stance,
              "posture": posture, "counter_evidence": mv_in.counter_evidence}
        if mv_in.confidence is not None:
            mv["confidence"] = mv_in.confidence
    else:
        mv_refs = [mkt, inst]
        mv = {"basis_refs": mv_refs,
              "logic": ("當日 Agent 輸出未通過驗證（已重試 3 次），依規則降級為保底方案：不做 tilt、只做合規基準必要調整；"
                        "市場面僅參考名單整體表現與法人買賣超，不改變既定的曝險方向。"),
              "regime": "neutral", "stance": "neutral", "posture": posture, "counter_evidence": None}

    used = set(mv_refs) | {p for i in infs for p in i["premises"]}
    sources, observations, omap = prune(ev, used)
    mv["basis_refs"] = [omap[r] for r in mv_refs]
    inferences = []
    for i in infs:
        inf = {"inf_id": i["inf_id"], "premise_refs": [omap[p] for p in i["premises"]], "logic": i["logic"],
               "counter_evidence": i["counter_evidence"]}
        if i["conclusion"]:
            inf["conclusion"] = i["conclusion"]
        if i["confidence"] is not None:
            inf["confidence"] = i["confidence"]
        inferences.append(inf)

    return {
        "schema_version": "4.2", "doc_type": "D-Plan", "team_id": ctx.team_id, "trade_date": ctx.trade_date,
        "sources": sources, "observations": observations, "market_view": mv, "inferences": inferences,
        "decisions": decisions, "no_trade_decisions": no_trade,
        "orders": [o.to_dict() for o in plan.orders],
        "agent_metadata": {"model_provider": model_provider, "model_version": model_version[:60],
                           "run_started_at": iso(started), "run_completed_at": iso(completed),
                           "code_version": code_version(), "input_tokens": usage.get("input", 0),
                           "output_tokens": usage.get("output", 0)},
    }


# ----------------------------------------------------------------------------- 主流程

def _next_state(state: TiltState, ctx: DayContext, ev, prop: Proposal | None, carried: dict[str, ActiveTilt],
                rebalanced: bool, fallback: bool) -> TiltState:
    tilts = dict(carried)
    if prop is not None:
        for c in prop.tilts:
            if abs(c.delta) < 1e-12:
                tilts.pop(c.ticker, None)
                continue
            payloads = [ev.payload(r) for r in c.obs_ids]
            tilts[c.ticker] = ActiveTilt(
                ticker=c.ticker, delta=c.delta, since=ctx.trade_date, basis=c.basis, logic=c.logic,
                counter_evidence=c.counter_evidence, evidence=payloads,
                evidence_as_of=max(p["as_of"] for p in payloads), confidence=c.confidence)
    return TiltState(tilts=tilts, last_rebalance=ctx.trade_date if rebalanced else state.last_rebalance,
                     as_of=ctx.trade_date, last_fallback=ctx.trade_date if fallback else state.last_fallback)


def assemble_day(ctx: DayContext, llm, state: TiltState | None = None, *, max_attempts: int = MAX_ATTEMPTS,
                 clock=now_taipei) -> AssembleResult:
    state = state or TiltState()
    decl = ctx.declaration or load_declaration()
    P = ctx.strategy.params
    started = clock()
    D, T = ctx.prev_date, ctx.trade_date
    close, nav, cash, holdings = ctx.prev_close, ctx.prev_nav, ctx.cash, dict(ctx.holdings)
    fdf, cap = factors_on(ctx.factors, D), ctx.caps.loc[D]
    cur_w = {t: n * close[t] / nav for t, n in holdings.items() if t in close}

    base, liquid, base_notes = base_plan(fdf, cap, P, ctx.top10)
    eligible = set(liquid)
    sys_notes: list[str] = list(base_notes)
    carried = _carried(state, base, eligible, P, sys_notes)
    ev = build_evidence(ctx.panel, D, inputs=ctx.inputs, focus_tickers=sorted(set(holdings) | set(base) | set(carried)),
                        carried={t: a.evidence for t, a in carried.items()}, fetched_at=ctx.fetched_at, declaration=decl)

    elapsed = trading_days_elapsed(ctx.calendar, state.last_rebalance, T) if state.last_rebalance else None
    sched_due = state.last_rebalance is None or not holdings or elapsed >= P.rebalance_every
    guard = bool(holdings) and ctx.strategy.force_rebalance(cur_w, ctx.top10)

    usage = {"input": 0, "output": 0}
    attempts: list[dict] = []
    feedback: list[str] = []
    vctx = ctx.validate_ctx()
    provider = getattr(llm, "provider", "other")
    model = getattr(llm, "model", "unknown")

    def finish(prop, merged, tilts_changed, fallback) -> tuple[dict | None, list[str], bool, Plan | None, str]:
        due = sched_due or guard or (tilts_changed and not fallback)
        reason = ("首次建倉" if not holdings else
                  f"距上次再平衡 {elapsed} 個交易日" if sched_due else
                  "漂移保護觸發（權重逼近上限或 Active Share 低於 22%）" if guard else "tilt 與前一日不同")
        if due:
            res = ctx.strategy.decide(fdf, cap, cur_w, ctx.top10, merged or None)
            target = dict(res.weights)
        else:
            target = dict(cur_w)
        if due:
            plan = plan_orders(target, nav, cash, close, holdings)
        else:
            plan = Plan(no_trade=sorted(holdings), cash_after=cash)
        merged_final = dict(merged)
        completed = clock()
        doc = build_doc(ctx, ev, prop=prop, carried=carried, merged=merged_final, plan=plan, due=due, due_reason=reason,
                        usage=usage, started=started, completed=completed, model_provider=provider, model_version=model)
        errs: list[str] = []
        if prop is not None:
            intent = doc["market_view"]["posture"]["net_exposure_intent"]
            st = doc["market_view"]["stance"]
            if (st == "defensive" and intent == "increase") or (st == "aggressive" and intent == "reduce"):
                errs.append(f"stance={st} 與實際委託方向不符：委託淨流向使 posture 變成 {intent}。"
                            "請調整 tilt（例如減少淨加碼）或改 stance")
        rep = validate(doc, vctx, filename=f"D-Plan_{ctx.team_id}_{T}.json")
        if rep.errors and all(e.startswith("[C14]") for e in rep.errors):          # 自癒：缺口不存在 → 拿掉 funding_for
            doc = build_doc(ctx, ev, prop=prop, carried=carried, merged=merged_final, plan=plan, due=due,
                            due_reason=reason, usage=usage, started=started, completed=clock(),
                            model_provider=doc["agent_metadata"]["model_provider"], model_version=model, with_funding=False)
            rep = validate(doc, vctx, filename=f"D-Plan_{ctx.team_id}_{T}.json")
        errs += rep.errors
        return (doc if not errs else None), errs, due, plan, rep

    last_errors: list[str] = []
    for n in range(1, max_attempts + 1):
        system, user = build_prompts(ctx, decl, ev, base, carried, ev.carried_ids, feedback)
        log = {"attempt": n, "errors": []}
        attempts.append(log)
        try:
            res = llm.generate(system, user)
        except LLMError as e:
            log["errors"] = feedback = [f"LLM 呼叫失敗：{e}"]
            continue
        usage["input"] += res.input_tokens
        usage["output"] += res.output_tokens
        log["raw"] = res.text
        prop, errs = parse_proposal(res.text)
        if errs:
            log["errors"] = feedback = errs
            continue
        errs = check_proposal(prop, ctx, ev, carried, decl)
        if errs:
            log["errors"] = feedback = errs
            continue
        merged = {t: a.delta for t, a in carried.items()}
        for c in prop.tilts:
            if abs(c.delta) < 1e-12:
                merged.pop(c.ticker, None)
            else:
                merged[c.ticker] = c.delta
        try:
            apply_tilts(base, merged, params=P, eligible=eligible, top10=ctx.top10, strict=True)
        except TiltError as e:
            log["errors"] = feedback = [f"apply_tilts：{m}" for m in e.errors]
            continue
        tilts_changed = merged != state.deltas()
        doc, errs, due, plan, rep = finish(prop, merged, tilts_changed, fallback=False)
        if errs:
            log["errors"] = feedback = errs
            continue
        new_state = _next_state(state, ctx, ev, prop, carried, due, False)
        return AssembleResult(doc, rep, False, attempts, new_state, due, usage, sys_notes + (plan.notes if plan else []))

    # --- 保底：零 tilt、只做基準必要調整
    doc, errs, due, plan, rep = finish(None, {}, False, fallback=True)
    if errs:
        raise AssembleError("保底 D-Plan 也未通過驗證：\n" + "\n".join(errs))
    new_state = _next_state(state, ctx, ev, None, carried, due, True)
    return AssembleResult(doc, rep, True, attempts, new_state, due, usage,
                          sys_notes + ["fallback_zero_tilt"] + (plan.notes if plan else []))
