"""正式 D-Plan 組裝器（v2：集中主題＋事件驅動）。

流程（assemble_conviction_day）：
  1. 準備（只用決策日 D 收盤後資料）：候選池 screen、目前高信心名單（runs/state/conviction.json ∩ 主辦方庫存）、
     持有天數與相對成本（停損）、高波動事件前一日判斷、Active Share 硬規則。
  2. 證據：行情、法人、夜盤、美股、事件、候選池（candidate_screen + trust_flow）、部位成本（position_pnl）、沿用中的舊證據。
  3. LLM（最多 3 次）：輸出 market_view、picks（10–12 檔＋高／中）、exits。逐層檢查，錯誤回饋 LLM 重寫：
       解析 → 引用（ticker／basis／authority／時間）→ 換股紀律（持有 3 日、每日 4 檔、停損、事件前一日）
       → build_portfolio（權重換算、湊檔股、現金、Active Share）→ stance 與委託方向 → validate() 的 ERROR。
  4. 三次都失敗 → 降級：已有高信心名單 → 凍結（沿用現有部位，只做停損與漂移保護）；沒有名單或凍結也不合規
     → 合規基準 A（舊版正式策略 FinalStrategy.decide，零 tilt）。保底也必須通過 validate，否則 AssembleError、不輸出。
LLM 只決定「哪幾檔、高或中」；權重由程式換算，委託由 derive_orders 機械推導。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..config import ACTIVE_SHARE_MIN, TSMC
from ..data.etf_holdings import check_target_weights
from ..dplan_validate import Report, validate
from ..strategy.candidates import CandidateParams, screen
from ..strategy.conviction import (LEVEL_LABELS, ConvictionParams, HeldPick, apply_band, blackout_events,
                                   build_portfolio, discipline, guard_triggered, pick_return, stop_loss_names)
from ..strategy.declaration import allowed_bases, load_declaration
from ..strategy.factors import factors_on
from ..universe import load_universe
from .assemble import EVIDENCE_CUTOFF_TIME, PROMPT_DIR, AssembleError, DayContext, _render, code_version, prompt_version
from .conviction_state import ConvictionState, Pick
from .llm import LLMError, PicksProposal, parse_picks
from .observations import (BASIS_LABELS, EvidenceBuilder, blackout_observation, build_evidence,
                           candidate_observations, filler_observation, iso, now_taipei, position_observation, prune)
from .plan import BUY_BUFFER, Plan, assign_funding, plan_orders, posture_for
from .tilt_state import trading_days_elapsed

MAX_ATTEMPTS = 3
EVENT_BASES = {"financial_report", "investor_conference", "monthly_revenue", "material_news"}
SYSTEM_PROMPT, USER_PROMPT = "conviction_system.md", "conviction_user.md"


@dataclass
class DayResult:
    doc: dict
    report: Report
    mode: str                                      # llm | hold | base
    attempts: list[dict]
    state: ConvictionState
    usage: dict
    notes: list[str]

    @property
    def fallback(self) -> bool:
        return self.mode != "llm"

    @property
    def filename(self) -> str:
        return f"D-Plan_{self.doc['team_id']}_{self.doc['trade_date']}.json"


def write_conviction_dplan(result: DayResult, outdir: Path, state_path: Path | None = None) -> Path:
    """寫檔前再確認沒有 ERROR；寫檔後才更新狀態檔。"""
    if not result.report.ok:
        raise AssembleError("D-Plan 有 ERROR，拒絕寫檔：\n" + str(result.report))
    outdir.mkdir(parents=True, exist_ok=True)
    p = outdir / result.filename
    p.write_text(json.dumps(result.doc, ensure_ascii=False, indent=2), encoding="utf-8")
    result.state.save(state_path)
    return p


def _name(t: str) -> str:
    return load_universe().get(t, {}).get("name", t)


# ----------------------------------------------------------------------------- 準備

@dataclass
class Prep:
    D: str
    T: str
    close: dict[str, float]
    nav: float
    cash: float
    holdings: dict[str, int]
    cur_w: dict[str, float]
    fdf: pd.DataFrame
    cap: pd.Series
    elig: set[str]
    held_picks: dict[str, Pick]
    held: dict[str, HeldPick]
    stop: set[str]
    blackout: list[dict]
    cands: pd.DataFrame
    ev: object
    cand_obs: dict[str, dict[str, str]]
    pos_obs: dict[str, str]
    blk_obs: str | None
    ap_target: float
    forced: bool
    notes: list[str] = field(default_factory=list)


def prepare(ctx: DayContext, state: ConvictionState, P: ConvictionParams, C: CandidateParams,
            events: list[dict] | None, decl: dict) -> Prep:
    D, T = ctx.prev_date, ctx.trade_date
    close, nav, cash, holdings = ctx.prev_close, ctx.prev_nav, ctx.cash, {t: n for t, n in ctx.holdings.items() if n}
    cur_w = {t: n * close[t] / nav for t, n in holdings.items() if t in close}
    fdf, cap = factors_on(ctx.factors, D), ctx.caps.loc[D]
    elig = set(fdf.index[(fdf["adv20"] >= P.min_adv) & cap.reindex(fdf.index).notna()
                         & pd.Series({t: t in close for t in fdf.index})])
    held_picks = {t: p for t, p in state.picks.items() if holdings.get(t, 0) > 0}
    cal = ctx.calendar
    held = {t: HeldPick(p.level, trading_days_elapsed(cal, p.since, T), pick_return(ctx.panel, t, p.since, D))
            for t, p in held_picks.items()}
    stop = stop_loss_names(held, P)
    blk = blackout_events(T, events)
    cands = screen(fdf, ctx.top15, C, held=set(held_picks))
    ev = build_evidence(ctx.panel, D, inputs=ctx.inputs, carried={t: p.evidence for t, p in held_picks.items()},
                        fetched_at=ctx.fetched_at, declaration=decl)
    b = EvidenceBuilder(ctx.fetched_at, ev)
    cand_obs = candidate_observations(b, ctx.panel, D, cands)
    pos_obs = {t: position_observation(b, D, t, held_picks[t].since, held[t].ret, held[t].days_held, held[t].level)
               for t in held_picks}
    blk_obs = blackout_observation(b, blk, D, T) if blk else None
    cur_ap = (check_target_weights(cur_w, ctx.top15, margin=0.0, expected_etfs=list(ctx.top15))["min"]
              if ctx.top15 and cur_w else None)
    ap_target = P.ap_recovery_target if cur_ap is not None and cur_ap < ACTIVE_SHARE_MIN else P.ap_target
    forced = cur_ap is not None and cur_ap < P.ap_trigger
    notes = (["ap_recovery"] if ap_target != P.ap_target else []) + (["ap_forced_rebalance"] if forced else [])
    return Prep(D, T, close, nav, cash, holdings, cur_w, fdf, cap, elig, held_picks, held, stop, blk, cands, ev,
                cand_obs, pos_obs, blk_obs, ap_target, forced, notes)


# ----------------------------------------------------------------------------- prompt

def _decl_summary(decl: dict) -> str:
    d = {"etf": decl["etf"]["name"], "theme": decl["theme"]["summary"], "philosophy": decl["philosophy"],
         "conviction_levels": decl["selection"]["conviction_levels"], "discipline": decl["discipline"],
         "allowed_bases": [{"id": b["id"], "label": b["label"], "authority": b["authority"]} for b in allowed_bases(decl)],
         "requirements": decl["selection"]["requirements"]}
    return json.dumps(d, ensure_ascii=False, indent=1)


def build_prompts(ctx: DayContext, prep: Prep, decl: dict, P: ConvictionParams, feedback: list[str]) -> tuple[str, str]:
    bases = "\n".join(f"- {b['id']}（{b['label']}）：來源 authority 須為 {'／'.join(b['authority'])}" for b in allowed_bases(decl))
    system = _render((PROMPT_DIR / SYSTEM_PROMPT).read_text(encoding="utf-8"), etf_name=decl["etf"]["name"],
                     allowed_bases=bases, n_min=P.n_conv_min, n_max=P.n_conv_max, w_high=f"{P.w_high:.0%}",
                     w_mid=f"{P.w_mid:.1%}", w_tsmc_high=f"{P.w_tsmc_high:.0%}", w_tsmc_mid=f"{P.w_tsmc_mid:.0%}",
                     min_hold_days=P.min_hold_days, max_swaps=P.max_swaps, stop_loss=f"{P.stop_loss:.0%}",
                     keep_rank=P.keep_rank)
    cur = []
    for t, p in prep.held_picks.items():
        h = prep.held[t]
        ok = h.days_held >= P.min_hold_days
        line = (f"- {t} {_name(t)}：{LEVEL_LABELS[p.level]}信心（{p.level}），自 {p.since} 起，已持有 {h.days_held} 個交易日"
                f"（{'可移出／可降級' if ok else '未滿 3 日，不可移出或降級'}）；"
                + (f"相對成本 {h.ret * 100:+.1f}%；" if h.ret is not None else "")
                + (f"候選名次 {int(prep.cands.at[t, 'rank'])}（≤ {P.keep_rank}：預設續抱）；"
                   if t in prep.cands.index and prep.cands.at[t, "rank"] <= P.keep_rank else
                   f"已跌出候選前 {P.keep_rank} 名；")
                + f"原依據 {BASIS_LABELS.get(p.basis, p.basis)}；原證據 obs_id：{prep.ev.carried_ids.get(t, [])}；"
                f"部位 obs_id：{prep.pos_obs[t]}")
        if t in prep.stop:
            line += f"；⚠ 觸發停損（跌幅 > {P.stop_loss:.0%}）：須移出，除非引用今日新的正面事件"
        cur.append(line)
    cl = []
    for t, r in prep.cands.iterrows():
        o = prep.cand_obs[t]
        tag = "持有中" if r["held"] else ("2330 固定列入" if r["forced"] and not r["in_pool"] else "")
        cl.append(f"- 第 {int(r['rank'])} 名 {t} {_name(t)}{'（' + tag + '）' if tag else ''}：分數 {r['score']:+.2f}；"
                  f"candidate_screen {o['screen']}" + (f"；trust_flow {o['trust']}" if "trust" in o else ""))
    obs = []
    for o in prep.ev.observations:
        m = prep.ev.meta[o["obs_id"]]
        obs.append(json.dumps({"obs_id": o["obs_id"], "topic": o.get("topic"), "ticker": m.get("ticker"),
                               "authority": m.get("authorities"), "as_of": m["as_of"], "carried": m.get("carried"),
                               "statement": o["statement"], "values": o["values"]}, ensure_ascii=False))
    if prep.blackout:
        blk = (f"⚠ 今天是高波動事件前一交易日（{'、'.join(e['name'] for e in prep.blackout)}，美國時間 {prep.blackout[0]['date']}，"
               f"見 {prep.blk_obs}）：不可新增股票、不可升級，既有部位凍結")
    else:
        blk = "今天不是高波動事件前一交易日"
    fb = ("# 上一次輸出的問題（請修正後重新輸出完整 JSON）\n" + "\n".join(f"- {e}" for e in feedback)) if feedback else ""
    user = _render((PROMPT_DIR / USER_PROMPT).read_text(encoding="utf-8"), trade_date=ctx.trade_date,
                   prev_date=ctx.prev_date, prompt_version=prompt_version(), blackout=blk,
                   declaration_summary=_decl_summary(decl),
                   current_picks="\n".join(cur) or "（目前沒有高信心名單：今天是首次建倉，請挑 10–12 檔）",
                   candidates="\n".join(cl), observations="\n".join(obs), feedback=fb)
    return system, user


# ----------------------------------------------------------------------------- 提案檢查

def check_picks(prop: PicksProposal, ctx: DayContext, prep: Prep, state: ConvictionState, decl: dict,
                P: ConvictionParams) -> tuple[list[str], set[str]]:
    """引用與紀律檢查。回傳 (errors, new_positive：停損股中引用了今日新正面事件而可保留者)。"""
    ev = prep.ev
    errs: list[str] = []
    ids = {o["obs_id"] for o in ev.observations}
    for r in prop.market_view.basis_refs:
        if r not in ids:
            errs.append(f"market_view.basis_refs 引用不存在的 {r}")
    cutoff = f"{ctx.prev_date}T{EVIDENCE_CUTOFF_TIME}"
    fresh = f"{state.prev_date}T{EVIDENCE_CUTOFF_TIME}" if state.prev_date else ""
    allowed = {b["id"]: set(b["authority"]) for b in allowed_bases(decl)}
    pool = set(prep.cands.index) | set(prep.held_picks)
    new_positive: set[str] = set()
    for c in prop.picks:
        w = f"picks {c.ticker}"
        if c.ticker not in pool:
            errs.append(f"{w}：不在今日候選池，也不在目前高信心名單")
            continue
        missing = [r for r in c.obs_ids if r not in ids]
        if missing:
            errs.append(f"{w}：obs_ids 引用不存在的 {missing}")
            continue
        good = [r for r in c.obs_ids if ev.meta[r].get("basis") == c.basis and ev.meta[r].get("ticker") == c.ticker
                and set(ev.meta[r]["authorities"]) <= allowed[c.basis]]
        if not good:
            errs.append(f"{w}：obs_ids 中沒有任何一則是「{c.ticker} 的 {BASIS_LABELS[c.basis]}」事實"
                        f"（需 ticker={c.ticker}、topic={c.basis}、來源 authority ∈ {sorted(allowed[c.basis])}）")
            continue
        late = [r for r in c.obs_ids if ev.meta[r]["as_of"] > cutoff]
        if late:
            errs.append(f"{w}：{late} 的 content_as_of 晚於決策日 {cutoff}，不可引用")
        if c.ticker in prep.stop and any(ev.meta[r].get("basis") in EVENT_BASES and not ev.meta[r]["carried"]
                                         and ev.meta[r].get("ticker") == c.ticker and ev.meta[r]["as_of"] > fresh
                                         for r in c.obs_ids):
            new_positive.add(c.ticker)
    _, derrs, _ = discipline(prop.levels(), prep.held, P, blackout=bool(prep.blackout), new_positive=new_positive,
                             strict=True, eligible=prep.elig)
    errs += derrs
    stop = prep.stop - new_positive
    exits = {x.ticker: x for x in prop.exits}
    picked = {c.ticker for c in prop.picks}
    for t in prep.held_picks:
        if t not in picked and t not in stop and t not in exits:
            errs.append(f"{t} 目前在高信心名單但不在 picks：請在 exits 寫出移出理由（引用 obs_id），或把它留在 picks")
    ranks = prep.cands["rank"].to_dict()
    for t, x in exits.items():
        if t in prep.held_picks and t in ranks and ranks[t] <= P.keep_rank:      # 遲滯：仍在前 30 名 → 要有新的依據事實才可移出
            fresh_ids = [r for r in x.obs_ids if r in ids and ev.meta[r].get("ticker") == t and ev.meta[r].get("basis")
                         and not ev.meta[r]["carried"]]
            if not fresh_ids:
                errs.append(f"exits {t}：候選名次 {int(ranks[t])} ≤ {P.keep_rank}，預設續抱；要移出必須引用該股今日的依據事實"
                            "（財報、法說會、月營收、投信、重大訊息的 observation），候選分數、股價或部位損益不構成移出理由")
        if t not in prep.held_picks:
            errs.append(f"exits {t}：不在目前高信心名單")
        bad = [r for r in x.obs_ids if r not in ids]
        if bad:
            errs.append(f"exits {t}：obs_ids 引用不存在的 {bad}")
        elif any(ev.meta[r]["as_of"] > cutoff for r in x.obs_ids):
            errs.append(f"exits {t}：引用的 observation 晚於決策日 {cutoff}")
    return errs, new_positive


# ----------------------------------------------------------------------------- D-Plan

def build_doc(ctx: DayContext, prep: Prep, *, mode: str, prop: PicksProposal | None, levels: dict[str, str],
              target: dict[str, float], fillers: list[str], plan: Plan, usage: dict, started: datetime,
              completed: datetime, provider: str, model: str, stop_exits: set[str], with_funding: bool = True,
              base_note: str = "") -> dict:
    P_band = ConvictionParams().band
    nav, close, D, ev = prep.nav, prep.close, prep.D, prep.ev
    b = EvidenceBuilder(ctx.fetched_at, ev)
    mkt = ev.by_topic("tw_market")[0]["obs_id"]
    inst = ev.by_topic("inst_flow")[0]["obs_id"]
    decisions = [{"decision_id": f"D{i}", **d} for i, d in enumerate(plan.decisions, 1)]
    cur = prep.cur_w
    post = dict(prep.holdings)
    for o in plan.orders:
        post[o.ticker] = post.get(o.ticker, 0) + (o.shares if o.side == "BUY" else -o.shares)
    n_post = sum(1 for n in post.values() if n > 0)
    posture = posture_for(plan, nav)
    if with_funding:
        assign_funding(decisions, plan, nav, close, posture["target_cash_pct_range"][0])

    mk_src = [s["source_id"] for s in ev.sources if s["authority"] in ("twse", "tpex") and "日行情" in s.get("name", "")
              and s["content_as_of"].startswith(D)]
    traded_dev = [abs(d["target_weight"] - cur.get(d["ticker"], 0.0)) for d in decisions]
    dev_values = {"n_holdings": len(cur), "n_trades": len(decisions), "no_trade_band_pct": P_band * 100,
                  "max_traded_dev_pct": max(traded_dev, default=0.0) * 100,
                  "target_cash_pct": (1.0 - sum(target.values())) * 100}
    dev_obs = b.add_obs(mk_src[:5], "weights_vs_target",
                        f"依 {D} 收盤價與前日淨值計算：持股 {len(cur)} 檔，本日調整 {len(decisions)} 檔、維持 {len(plan.no_trade)} 檔；"
                        f"調整檔的權重變動最大 {dev_values['max_traded_dev_pct']:.2f}%，目標現金 {dev_values['target_cash_pct']:.2f}%",
                        dev_values, as_of=f"{D}T13:30:00+08:00")
    filler_obs = None
    if fillers:
        fw = sum(target.get(t, 0.0) for t in fillers) / len(fillers)
        filler_obs = filler_observation(b, D, fillers, fw, ConvictionParams().filler_pool)

    infs: list[dict] = []

    def add_inf(premises, logic, conclusion=None, ce=None, conf=None) -> str:
        infs.append({"inf_id": f"I{len(infs) + 1}", "premises": list(dict.fromkeys(premises)), "logic": logic[:1000],
                     "conclusion": (conclusion or "")[:300] or None, "counter_evidence": ce, "confidence": conf})
        return infs[-1]["inf_id"]

    touched = {d["ticker"] for d in decisions} | set(plan.no_trade)
    role_inf: dict[str, str] = {}
    picks_in = {c.ticker: c for c in (prop.picks if prop else [])}
    for t in sorted(levels, key=lambda x: -target.get(x, 0.0)):
        if t not in touched:
            continue
        w = target.get(t, cur.get(t, 0.0))
        concl = f"{_name(t)} 列為{LEVEL_LABELS[levels[t]]}信心部位（目標約 {w:.1%}）"
        pos = [prep.pos_obs[t]] if t in prep.pos_obs else []
        if t in picks_in:
            c = picks_in[t]
            role_inf[t] = add_inf(list(c.obs_ids) + pos, c.logic, concl, c.counter_evidence, c.confidence)
        else:
            p = prep.held_picks.get(t)
            prem = list(ev.carried_ids.get(t, [])) + pos
            logic = (f"Agent 今日輸出未通過檢查，依降級規則凍結名單：沿用 {p.since} 起以{BASIS_LABELS.get(p.basis, p.basis)}"
                     f"列入的{LEVEL_LABELS[p.level]}信心部位（{p.logic.rstrip('。.')}）；今日不新增也不移出，只做停損與漂移保護。"
                     if p else "沿用既有高信心部位。")
            role_inf[t] = add_inf(prem or [dev_obs], logic, concl, p.counter_evidence if p else None,
                                  p.confidence if p else None)
    if prop:
        for x in prop.exits:
            if x.ticker in touched:
                pos = [prep.pos_obs[x.ticker]] if x.ticker in prep.pos_obs else []
                role_inf[x.ticker] = add_inf(list(x.obs_ids) + pos, x.logic, f"{_name(x.ticker)} 移出高信心名單")
    for t in sorted(stop_exits & touched):
        h = prep.held[t]
        role_inf[t] = add_inf([prep.pos_obs[t]],
                              f"{_name(t)} 收盤相對成本 {h.ret * 100:+.1f}%，跌幅超過停損線 {ConvictionParams().stop_loss:.0%}，"
                              "今日沒有新的正面事件可推翻，依換股紀律出場，避免單檔虧損擴大。",
                              f"{_name(t)} 觸發停損，出清")
    fill_inf = None
    if filler_obs and any(t in touched for t in fillers):
        fill_inf = add_inf([filler_obs],
                           f"湊檔數部位：高信心名單之外，取市值前 {ConvictionParams().filler_pool} 大中市值最大的 {len(fillers)} 檔、每檔約 "
                           f"{sum(target.get(t, 0.0) for t in fillers) / len(fillers):.1%}，使總持股維持 21–24 檔並分散單一事件風險；"
                           "這些部位不代表看多，只是滿足持股檔數規則的低成本配置。",
                           f"湊檔數部位 {len(fillers)} 檔")
    base_inf = None
    if mode == "base":
        base_inf = add_inf([mkt, dev_obs],
                           "Agent 今日輸出連續 3 次未通過檢查，且沒有可凍結的高信心名單，依降級規則改用合規基準 A："
                           "市值加權（2330 ≤ 20%、個股 ≤ 8%）取前 28 檔、Active Share 補到 27% 以上、現金約 3%；"
                           "權重由程式計算，委託依官方公式推導。" + base_note, "依合規基準 A 配置")
    rotate_inf = None
    for t in sorted(touched):
        if t in role_inf or (t in fillers and fill_inf) or base_inf:
            continue
        if rotate_inf is None:
            rotate_inf = add_inf([dev_obs] + ([filler_obs] if filler_obs else []),
                                 "不在今日的高信心名單、也不在湊檔數部位（市值前 30 大中排序較後或已列入高信心）的既有持股，"
                                 "依目標組合出清或維持至可交易，資金轉入目標組合。", "非目標組合的持股出清")
        role_inf[t] = rotate_inf
    blk_inf = None
    if prep.blackout and prep.blk_obs and plan.no_trade:
        blk_inf = add_inf([prep.blk_obs],
                          f"{prep.blackout[0]['name']} 將在今日台股收盤後揭曉，屬高波動事件；依紀律前一交易日不新增部位、不升級，"
                          "既有部位凍結，只做停損與漂移保護。", "事件前一交易日：不新增部位")
    rebalance_inf = notrade_inf = None
    if decisions:
        why = {"llm": "依今日高信心名單與信心等級換算目標權重（高 8%、中 5.5%，2330 高 15%、中 10%）",
               "hold": "降級凍結：沿用現有部位，只處理停損與漂移保護",
               "base": "降級保底：改用合規基準 A 的目標權重"}[mode]
        rebalance_inf = add_inf([dev_obs], f"{why}；與現權重比較，新增、移出或偏離超過無交易帶 {P_band:.1%} 者才調整；"
                                           "委託由官方公式機械推導，委託後現金比預留買進價差緩衝。",
                                f"調整 {len(decisions)} 檔以回到目標組合")
    if plan.no_trade:
        notrade_inf = add_inf([dev_obs], f"其餘持股與目標權重差距在無交易帶 {P_band:.1%} 內（或當日無法交易），維持原部位，"
                                         "不為微小偏離付出交易成本（往返約 0.6%）。", f"維持 {len(plan.no_trade)} 檔不動")

    for d in decisions:
        t = d["ticker"]
        refs = [role_inf[t]] if t in role_inf else []
        if t in fillers and fill_inf and t not in role_inf:
            refs.append(fill_inf)
        if base_inf:
            refs.append(base_inf)
        d["inference_refs"] = list(dict.fromkeys(refs + [rebalance_inf]))
        d["risk_check"] = (f"權重 {cur.get(t, 0.0):.2%} → {d['target_weight']:.2%}（上限 {'25%' if t == TSMC else '10%'}）；"
                           f"委託後持股 {n_post} 檔 ∈ [20,30]")[:300]
    no_trade = []
    for t in plan.no_trade:
        refs = [role_inf[t]] if t in role_inf else []
        if t in fillers and fill_inf and t not in role_inf:
            refs.append(fill_inf)
        if base_inf:
            refs.append(base_inf)
        if blk_inf:
            refs.append(blk_inf)
        no_trade.append({"ticker": t, "reason_refs": list(dict.fromkeys(refs + [notrade_inf])),
                         "reason": f"權重 {cur.get(t, 0.0):.2%} 與目標差距在無交易帶 {P_band:.1%} 內或當日無法交易，維持部位"[:300]})

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
              "logic": ("當日 Agent 輸出未通過檢查（已重試 3 次），依規則降級為"
                        + ("凍結既有高信心名單" if mode == "hold" else "合規基準 A")
                        + "；市場面僅參考名單整體表現與法人買賣超，不改變既定的曝險方向。"),
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
        "agent_metadata": {"model_provider": provider, "model_version": model[:60], "run_started_at": iso(started),
                           "run_completed_at": iso(completed), "code_version": code_version(),
                           "input_tokens": usage.get("input", 0), "output_tokens": usage.get("output", 0)},
    }


# ----------------------------------------------------------------------------- 主流程

def _targets(prep: Prep, res_weights: dict[str, float], P: ConvictionParams, top15, mode: str) -> tuple[dict, list[str]]:
    target, notes = dict(res_weights), []
    if mode != "base" and not prep.forced and not guard_triggered(prep.cur_w, P):
        target, notes = apply_band(target, prep.cur_w, P, top15, prep.ap_target)
    return target, notes


def assemble_conviction_day(ctx: DayContext, llm, state: ConvictionState | None = None, *,
                            params: ConvictionParams | None = None, cand_params: CandidateParams | None = None,
                            events: list[dict] | None = None, max_attempts: int = MAX_ATTEMPTS,
                            clock=now_taipei) -> DayResult:
    state = state or ConvictionState()
    P, C = params or ConvictionParams(), cand_params or CandidateParams()
    decl = ctx.declaration or load_declaration()
    started = clock()
    prep = prepare(ctx, state, P, C, events, decl)
    vctx = ctx.validate_ctx()
    provider, model = getattr(llm, "provider", "other"), getattr(llm, "model", "unknown")
    usage = {"input": 0, "output": 0}
    attempts: list[dict] = []
    feedback: list[str] = []

    def finish(mode, prop, levels, weights, fillers, stop_exits, base_note=""):
        target, bnotes = _targets(prep, weights, P, ctx.top15, mode)
        plan = plan_orders(target, prep.nav, prep.cash, prep.close, prep.holdings,
                           buy_buffer=BUY_BUFFER if mode != "base" else 0.0)
        kw = dict(mode=mode, prop=prop, levels=levels, target=target, fillers=fillers, plan=plan, usage=usage,
                  started=started, provider=provider, model=model, stop_exits=stop_exits, base_note=base_note)
        doc = build_doc(ctx, prep, completed=clock(), **kw)
        errs: list[str] = []
        if prop is not None:
            intent, st = doc["market_view"]["posture"]["net_exposure_intent"], doc["market_view"]["stance"]
            if (st == "defensive" and intent == "increase") or (st == "aggressive" and intent == "reduce"):
                errs.append(f"stance={st} 與實際委託方向不符：委託淨流向使 posture 變成 {intent}，請改 stance")
        fname = f"D-Plan_{ctx.team_id}_{ctx.trade_date}.json"
        rep = validate(doc, vctx, filename=fname)
        if rep.errors and all(e.startswith("[C14]") for e in rep.errors):          # 缺口不存在 → 拿掉 funding_for
            doc = build_doc(ctx, prep, completed=clock(), with_funding=False, **kw)
            rep = validate(doc, vctx, filename=fname)
        return doc, errs + rep.errors, rep, plan, bnotes

    for n in range(1, max_attempts + 1):
        system, user = build_prompts(ctx, prep, decl, P, feedback)
        log: dict = {"attempt": n, "errors": []}
        attempts.append(log)
        try:
            res = llm.generate(system, user)
        except LLMError as e:
            log["errors"] = feedback = [f"LLM 呼叫失敗：{e}"]
            continue
        usage["input"] += res.input_tokens
        usage["output"] += res.output_tokens
        log["raw"] = res.text
        prop, errs = parse_picks(res.text)
        if errs:
            log["errors"] = feedback = errs
            continue
        errs, new_pos = check_picks(prop, ctx, prep, state, decl, P)
        if errs:
            log["errors"] = feedback = errs
            continue
        levels = dict(prop.levels())
        stop_exits = (prep.stop - new_pos) & set(prep.held_picks)
        built = build_portfolio(levels, prep.cap, prep.elig, ctx.top15, P, current_fillers=state.fillers,
                                ap_target=prep.ap_target, current=prep.cur_w,
                                no_new=bool(prep.blackout) and bool(prep.cur_w),
                                exits=[t for t in prep.held_picks if t not in levels])
        if built.errors:
            log["errors"] = feedback = [f"組合檢查：{e}" for e in built.errors]
            continue
        doc, errs, rep, plan, bnotes = finish("llm", prop, levels, built.weights, built.fillers, stop_exits)
        if errs:
            log["errors"] = feedback = errs
            continue
        new_state = _next_state(state, ctx, prep, prop, levels, built.fillers, "llm")
        return DayResult(doc, rep, "llm", attempts, new_state, usage, prep.notes + built.notes + bnotes + plan.notes)

    # --- 降級 1：凍結既有高信心名單
    notes = list(prep.notes)
    if prep.held_picks:
        levels = {t: p.level for t, p in prep.held_picks.items() if t not in prep.stop}
        built = build_portfolio(levels, prep.cap, prep.elig, ctx.top15, P, current_fillers=state.fillers,
                                ap_target=prep.ap_target, current=prep.cur_w, no_new=True, exits=sorted(prep.stop))
        if not built.errors:
            doc, errs, rep, plan, bnotes = finish("hold", None, levels, built.weights, built.fillers,
                                                  prep.stop & set(prep.held_picks))
            if not errs:
                new_state = _next_state(state, ctx, prep, None, levels, built.fillers, "hold")
                return DayResult(doc, rep, "hold", attempts, new_state, usage,
                                 notes + ["fallback_hold"] + built.notes + bnotes + plan.notes)
            notes.append("fallback_hold_failed:" + "；".join(errs)[:300])
        else:
            notes.append("fallback_hold_failed:" + "；".join(built.errors)[:300])
    # --- 降級 2：合規基準 A（舊版正式策略，零 tilt）
    r = ctx.strategy.decide(prep.fdf, prep.cap, prep.cur_w, ctx.top15, None)
    doc, errs, rep, plan, _ = finish("base", None, {}, r.weights, [], set())
    if errs:
        raise AssembleError("保底 D-Plan 也未通過驗證：\n" + "\n".join(errs))
    new_state = _next_state(state, ctx, prep, None, {}, [], "base")
    return DayResult(doc, rep, "base", attempts, new_state, usage, notes + ["fallback_base_a"] + r.notes + plan.notes)


def _next_state(state: ConvictionState, ctx: DayContext, prep: Prep, prop: PicksProposal | None,
                levels: dict[str, str], fillers: list[str], mode: str) -> ConvictionState:
    T = ctx.trade_date
    picks: dict[str, Pick] = {}
    if mode == "llm":
        allowed = {c.ticker: c for c in prop.picks}
        for t, lv in levels.items():
            c = allowed[t]
            good = [r for r in c.obs_ids if prep.ev.meta[r].get("ticker") == t and prep.ev.meta[r].get("basis")]
            payloads = [prep.ev.payload(r) for r in good]
            old = prep.held_picks.get(t)
            picks[t] = Pick(ticker=t, level=lv, since=old.since if old else T, basis=c.basis, logic=c.logic,
                            counter_evidence=c.counter_evidence, evidence=payloads,
                            evidence_as_of=max(p["as_of"] for p in payloads), confidence=c.confidence)
    elif mode == "hold":
        picks = {t: prep.held_picks[t] for t in levels}
    return ConvictionState(picks=picks, fillers=list(fillers), as_of=T, prev_date=ctx.prev_date,
                           last_fallback=T if mode != "llm" else state.last_fallback, mode=mode)
