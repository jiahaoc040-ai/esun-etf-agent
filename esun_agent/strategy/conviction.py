"""正式策略 v2：集中主題＋事件驅動（高信心部位 10–12 檔 ＋ 湊檔數部位 ＋ 現金 2–5%）。

投組結構（build_portfolio）：
  高信心部位   LLM（回測時為候選池分數前 11 名）從候選池挑 10–12 檔並給信心等級：
               高 = 8%、中 = 5.5%（2330：高 15%、中 10%）。單檔硬上限 8%（官方 10%，留 2% 給價格漂移：買進當天漲停 +10%
               且淨值下跌時仍不超過 10%，避免被認定為「加碼造成超限」），2330 上限 20%。
  湊檔數部位   市值前 30 大、且不在高信心名單者，取市值最大的若干檔各 1–2%，使總持股 = 20–24 檔
               （現有湊檔股仍在前 30 大者優先保留，避免無謂換手）。挑選順序：低重疊（出現在主動型 ETF 前 15 大的比例
               ≤ filler_max_overlap）優先，同一組內依市值由大到小。內部下限 23 檔（比官方 20 檔多 3 檔緩衝：停牌買不到、
               停損出場或零股調整都不會跌破 20 檔）。
  現金         目標 3%，區間 2–5%。
  配比         高信心名目權重合計過大 → 等比例縮小；過小 → 在上限內等比例放大（必要時現金拉到 5%）。
  Active Share 前 15 大 vs 任一主動型 ETF 前 15 大，官方 ≥ 15%、內部目標 ≥ 20%：不足時先把重疊的湊檔股換成不重疊的，
               再把重疊最大的高信心部位每次降 1.5%（最低 3%），釋出的權重給不重疊的高信心部位／現金。
LLM 只給「哪幾檔、高或中」，權重全部由這裡機械換算；股數再由 orders.derive_orders 算。

換股紀律（discipline）：
  - 高信心部位持有 ≥ 3 個交易日才可賣出或降級（除非觸發停損）。
  - 每日最多新增 4 檔、主動移出 4 檔（首次建倉不受限；停損出場不計入）。
  - 停損：還原價相對成本（進場日成交均價）下跌 > 12%，且當日沒有新的正面事件 → 出場。
  - 高波動事件（NVIDIA 財報、美國大選、FOMC，見 config/event_calendar.json）前一交易日：不新增部位、不升級。
strict=True（LLM）時違規逐條回報給 LLM 重寫；strict=False（回測代理）時直接修正成合規。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from ..config import ACTIVE_SHARE_MIN, CASH_BUFFER_HIGH, CASH_BUFFER_LOW, MAX_HOLDINGS, MIN_HOLDINGS, ROOT, TSMC
from ..data.etf_holdings import check_target_weights
from ..universe import load_universe
from .portfolio import _waterfill

EVENT_CALENDAR_PATH = ROOT / "config" / "event_calendar.json"
LEVELS = ("high", "medium")
LEVEL_LABELS = {"high": "高", "medium": "中"}


class ConvictionError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("；".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class ConvictionParams:
    n_conv_min: int = 10
    n_conv_max: int = 12
    w_high: float = 0.08
    w_mid: float = 0.055
    w_tsmc_high: float = 0.15
    w_tsmc_mid: float = 0.10
    cap_other: float = 0.08               # 單檔硬上限（官方 10%，留 2% 給價格漂移）
    cap_tsmc: float = 0.20                # 2330（官方 25%）
    conv_floor: float = 0.03              # Active Share 修正時高信心部位最低降到這裡
    filler_pool: int = 30                 # 湊檔數部位：市值前 30 大
    filler_min: float = 0.01
    filler_max: float = 0.02
    n_total_min: int = 23                 # 官方 20：至少留 3 檔緩衝（停牌買不到、停損出場）
    filler_max_overlap: float = 0.5       # 湊檔股：出現在超過一半主動型 ETF 前 15 大者排在後面（低重疊優先）
    n_total_max: int = 24
    cash_low: float = 0.02
    cash_target: float = 0.03
    cash_high: float = 0.05
    ap_target: float = 0.20               # 內部目標（官方 15%）
    ap_trigger: float = 0.17              # 前日收盤 Active Share < 此值 → 當日必須再平衡到 ≥ ap_target
    ap_recovery_target: float = 0.25      # 前日收盤已 < 15%（已違規一日）→ 當日補到這個更高的目標
    min_hold_days: int = 3
    keep_rank: int = 30                   # 遲滯：已持有且候選名次 ≤ 30 者預設續抱；要移出須引用該股新的依據事實
    max_swaps: int = 4
    stop_loss: float = 0.12
    band: float = 0.015                   # 無交易帶：既有持股目標與現權重差 < 1.5% 不交易
    guard_other: float = 0.095            # 前日收盤權重超過即減回目標（官方 10%／25%）
    guard_tsmc: float = 0.22
    min_adv: float = 3e8                  # 高信心部位的流動性門檻（同候選池）

    def cap(self, t: str) -> float:
        return self.cap_tsmc if t == TSMC else self.cap_other

    def guard(self, t: str) -> float:
        return self.guard_tsmc if t == TSMC else self.guard_other

    def nominal(self, t: str, level: str) -> float:
        if t == TSMC:
            return self.w_tsmc_high if level == "high" else self.w_tsmc_mid
        return self.w_high if level == "high" else self.w_mid


@dataclass
class ConvictionResult:
    weights: dict[str, float]
    conviction: dict[str, float]
    fillers: list[str]
    notes: list[str] = field(default_factory=list)
    active_share: dict | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def cash(self) -> float:
        return 1.0 - sum(self.weights.values())

    @property
    def n_hold(self) -> int:
        return sum(1 for w in self.weights.values() if w > 1e-12)


# --------------------------------------------------------------------------- 湊檔數部位候選

def filler_candidates(cap: pd.Series, eligible: set[str], picks, p: ConvictionParams,
                      current_fillers=(), top15: dict | None = None) -> list[str]:
    """市值前 filler_pool 大（限 eligible）中不在 picks 者，依序：
    現有湊檔股（仍在前 30 大、且低重疊者，避免換手）→ 低重疊（主動型 ETF 前 15 大重疊比例 ≤ filler_max_overlap）
    依市值 → 高重疊依市值。"""
    from .candidates import etf_overlap_counts
    c = cap.reindex(sorted(eligible)).dropna()
    c = c[c > 0]
    top = [t for t in c.sort_values(ascending=False).index[:p.filler_pool] if t not in picks]
    counts, n_etf = etf_overlap_counts(top15)
    heavy = {t for t in top if n_etf and counts.get(t, 0) / n_etf > p.filler_max_overlap}
    cur = set(current_fillers)
    keep = [t for t in top if t in cur and t not in heavy]
    light = [t for t in top if t not in heavy and t not in cur]
    return keep + light + [t for t in top if t in heavy]


# --------------------------------------------------------------------------- 權重配置

def _fit(picks: dict[str, str], fill_order: list[str], p: ConvictionParams, notes: list[str]) -> tuple[dict, list, float]:
    nominal = {t: p.nominal(t, lv) for t, lv in picks.items()}
    caps = {t: p.cap(t) for t in picks}
    nc = len(picks)
    nf_min = max(p.n_total_min - nc, 0)
    nf_max = min(p.n_total_max - nc, len(fill_order))
    if nf_max < nf_min:
        raise ConvictionError([f"湊檔數部位候選只有 {len(fill_order)} 檔，無法湊到 {p.n_total_min} 檔"])
    cash = p.cash_target
    S = sum(nominal.values())
    R = 1.0 - cash - S
    if R < nf_min * p.filler_min - 1e-12:                    # 高信心部位太大 → 等比例縮小
        nf, fw = nf_min, p.filler_min
        k = (1.0 - cash - nf * fw) / S
        conv = {t: w * k for t, w in nominal.items()}
        notes.append(f"conviction_scaled:{k:.3f}")
    elif R > nf_max * p.filler_max + 1e-12:                  # 高信心部位太小 → 在上限內等比例放大
        nf, fw = nf_max, p.filler_max
        want = 1.0 - cash - nf * fw
        conv = _waterfill(nominal, want, caps) if nominal else {}
        short = want - sum(conv.values())
        if nominal:
            notes.append(f"conviction_scaled:{sum(conv.values()) / S:.3f}")
        if short > 1e-9:
            extra_cash = min(short, p.cash_high - cash)
            cash += extra_cash
            short -= extra_cash
            if short > 1e-9 and nf:
                fw += short / nf
                notes.append(f"filler_over_max:{fw:.4f}")
    else:
        nf = min(nf_max, max(nf_min, math.ceil(R / p.filler_max - 1e-9)))
        fw = R / nf if nf else 0.0
        conv = dict(nominal)
    fillers = fill_order[:nf]
    w = dict(conv)
    w.update({t: fw for t in fillers})
    return w, fillers, cash


def _ap_items(res: dict) -> list[dict]:
    return sorted(res["fails"] + res["warnings"], key=lambda x: x["min"])


def _fix_active_share(w: dict[str, float], picks: dict[str, str], fillers: list[str], spare: list[str],
                      top15: dict, p: ConvictionParams, target: float, notes: list[str], max_iter: int = 60):
    margin = target - ACTIVE_SHARE_MIN
    exp = list(top15)
    res = check_target_weights(w, top15, margin=margin, expected_etfs=exp)
    it = 0
    while (res["fails"] or res["warnings"]) and it < max_iter:
        it += 1
        item = _ap_items(res)[0]
        etf_names = set(top15[item["etf"]])
        acted = False
        for o in item["overlap_tickers"]:
            t = o["ticker"]
            if t in fillers:
                rep = next((s for s in spare if s not in etf_names), None)
                if rep is None:
                    continue
                spare.remove(rep)
                i = fillers.index(t)
                fillers[i] = rep
                w[rep] = w.pop(t)
                notes.append(f"ap_filler_swap:{t}->{rep}")
                acted = True
                break
            if t in picks and w[t] > p.conv_floor + 1e-9:
                cut = min(0.015, w[t] - p.conv_floor)
                recv = {s: p.cap(s) - w[s] for s in picks if s not in etf_names and p.cap(s) - w[s] > 1e-9}
                recv.update({s: p.filler_max - w[s] for s in fillers                 # 不重疊的湊檔股也可承接（至多 2%）
                             if s not in etf_names and p.filler_max - w[s] > 1e-9})
                room = sum(recv.values())
                give_cash = max(0.0, min(cut - room, p.cash_high - (1.0 - sum(w.values()))))
                if room + give_cash < cut - 1e-9:
                    continue
                w[t] -= cut
                rest = cut - give_cash
                if rest > 1e-12:
                    k = rest / room
                    for s, r in recv.items():
                        w[s] += r * k
                notes.append(f"ap_conviction_cut:{t}:{cut:.4f}")
                acted = True
                break
        if not acted:
            break
        res = check_target_weights(w, top15, margin=margin, expected_etfs=exp)
    return w, res


def build_portfolio(picks: dict[str, str], cap: pd.Series, eligible: set[str], top15: dict | None,
                    params: ConvictionParams | None = None, *, current_fillers=(), ap_target: float | None = None,
                    current: dict[str, float] | None = None, no_new: bool = False, exits=()) -> ConvictionResult:
    """picks = {ticker: "high"|"medium"}（依優先序）。cap = D 日市值。eligible = D 日有價且流動性足夠的名單。
    no_new=True（高波動事件前一日）：凍結——既有持股（仍在名單或仍是湊檔股）維持現權重、新名稱不買（持股不足 20 檔時例外補湊檔股），
    exits（停損等必須出場者）釋出的權重留在現金。回傳 ConvictionResult；errors 不為空表示不可用（strict 時回饋 LLM）。"""
    p = params or ConvictionParams()
    notes: list[str] = []
    errors: list[str] = []
    uni = load_universe()
    for t, lv in picks.items():
        if t not in uni:
            errors.append(f"{t} 不在 150 檔名單")
        elif t not in eligible and (current or {}).get(t, 0.0) <= 1e-12:     # 已持有者可續抱（流動性下降不強迫賣出）
            errors.append(f"{t} 決策日無價格或流動性不足，不可新增為高信心部位")
        if lv not in LEVELS:
            errors.append(f"{t} 的信心等級需為 high 或 medium")
    if errors:
        return ConvictionResult({}, {}, [], notes, None, errors)
    order = filler_candidates(cap, eligible, picks, p, current_fillers, top15)
    try:
        w, fillers, _ = _fit(picks, order, p, notes)
    except ConvictionError as e:
        return ConvictionResult({}, {}, [], notes, None, e.errors)
    spare = [t for t in order if t not in fillers]
    if no_new and current is not None:
        # 凍結：既有持股維持現權重（超過漂移保護者降回配置權重），新名稱不買；被移出者（停損）釋出的權重留在現金
        frozen = {t: (min(cw, w.get(t, p.cap(t))) if cw > p.guard(t) else cw)
                  for t, cw in current.items() if cw > 1e-12 and t not in set(exits)}
        n = len(frozen)
        for t in order:                                     # 持股不足下限 → 允許補最小的湊檔部位（官方規則優先）
            if n >= p.n_total_min:
                break
            if t not in frozen:
                frozen[t] = p.filler_min
                n += 1
                notes.append(f"no_new_filler_added:{t}")
        fillers = [t for t in frozen if t not in picks]
        cash = 1.0 - sum(frozen.values())
        if cash > CASH_BUFFER_HIGH:                          # 多檔同日停損：現金過高（官方 < 25%）→ 加回既有部位，不買新名稱
            room = {t: (p.cap(t) if t in picks else p.filler_max) - x for t, x in frozen.items()}
            room = {t: r for t, r in room.items() if r > 1e-9}
            need = min(cash - p.cash_target, sum(room.values()))
            if need > 0:
                k = need / sum(room.values())
                for t, r in room.items():
                    frozen[t] += r * k
                notes.append(f"no_new_cash_redeployed:{need:.4f}")
        w = frozen
        notes.append("no_new_positions")
    res = None
    if top15:
        target = ap_target if ap_target is not None else p.ap_target
        if no_new:
            res = check_target_weights(w, top15, margin=0.0, expected_etfs=list(top15))
        else:
            w, res = _fix_active_share(w, picks, fillers, spare, top15, p, target, notes)
            if res["min"] < target - 1e-9:
                notes.append(f"ap_target_unreached:{res['min']:.3f}")
    w = {t: x for t, x in w.items() if x > 1e-12}
    errors += check_portfolio(w, p, res, relaxed_cash=no_new)
    conv = {t: w[t] for t in picks if t in w}
    return ConvictionResult(w, conv, [t for t in fillers if t in w], notes, res, errors)


def check_portfolio(w: dict[str, float], p: ConvictionParams, ap: dict | None = None,
                    relaxed_cash: bool = False) -> list[str]:
    """持股、權重、現金、Active Share 的最終檢查（內部門檻）。"""
    errs = []
    n = sum(1 for x in w.values() if x > 1e-12)
    if not (max(p.n_total_min, MIN_HOLDINGS) <= n <= min(p.n_total_max, MAX_HOLDINGS)):
        errs.append(f"持股 {n} 檔，不在 {p.n_total_min}–{p.n_total_max} 檔")
    for t, x in w.items():
        lim = p.guard(t) if relaxed_cash else p.cap(t)          # 凍結日沿用現權重：價格漂移容許到漂移保護線
        if x > lim + 1e-9:
            errs.append(f"{t} 權重 {x:.2%} 超過上限 {lim:.1%}")
    cash = 1.0 - sum(w.values())
    lo, hi = (CASH_BUFFER_LOW, CASH_BUFFER_HIGH) if relaxed_cash else (p.cash_low, p.cash_high)   # 凍結日：沿用現權重，放寬到 1%–22%
    if not (lo - 1e-9 <= cash <= hi + 1e-9):
        errs.append(f"現金 {cash:.2%} 不在 {lo:.0%}–{hi:.0%}")
    if ap is not None and ap["min"] < ACTIVE_SHARE_MIN:
        errs.append(f"Active Share 最低 {ap['min']:.1%}（{ap['worst_etf']}）低於官方門檻 {ACTIVE_SHARE_MIN:.0%}："
                    "高信心名單與主動型 ETF 前 15 大重疊過高，請換掉部分重疊標的")
    return errs


# --------------------------------------------------------------------------- 無交易帶與再平衡判斷

def apply_band(target: dict[str, float], current: dict[str, float], p: ConvictionParams,
               top15: dict | None = None, ap_target: float | None = None) -> tuple[dict[str, float], list[str]]:
    """既有持股且目標 > 0、|目標 − 現權重| < band、且現權重未超過漂移保護 → 沿用現權重（不交易），差額由現金吸收。
    沿用後現金跑出 [cash_low, cash_high] → 依偏離由大到小逐檔解除沿用，直到現金回到區間；
    Active Share 跌破目標 → 放棄無交易帶。"""
    out = dict(target)
    locked = sorted((t for t, w in target.items() if w > 0 and t in current and abs(w - current[t]) < p.band
                     and current[t] <= p.guard(t)), key=lambda t: (-abs(target[t] - current[t]), t))
    for t in locked:
        out[t] = current[t]
    notes = []
    cash = 1.0 - sum(out.values())
    while locked and not (p.cash_low - 1e-9 <= cash <= p.cash_high + 1e-9):
        t = locked.pop(0)
        out[t] = target[t]
        cash = 1.0 - sum(out.values())
        notes = ["band_unlocked_for_cash"]
    if top15:
        r = check_target_weights(out, top15, margin=0.0, expected_etfs=list(top15))
        if r["min"] < (ap_target if ap_target is not None else p.ap_target) - 1e-9:
            return dict(target), ["band_dropped_for_active_share"]
    return out, notes


def guard_triggered(current: dict[str, float], p: ConvictionParams) -> list[str]:
    return [t for t, w in current.items() if w > p.guard(t)]


# --------------------------------------------------------------------------- 換股紀律

@dataclass
class HeldPick:
    level: str
    days_held: int                         # (進場交易日, 今天] 之間的交易日數
    ret: float | None = None               # 還原價相對成本的報酬（停損用）


def stop_loss_names(held: dict[str, HeldPick], p: ConvictionParams) -> set[str]:
    return {t for t, h in held.items() if h.ret is not None and h.ret < -p.stop_loss}


def discipline(proposal: list[tuple[str, str]], held: dict[str, HeldPick], p: ConvictionParams, *,
               blackout: bool = False, new_positive: set[str] = frozenset(), strict: bool = True,
               eligible: set[str] | None = None) -> tuple[dict[str, str], list[str], list[str]]:
    """proposal = 依優先序的 [(ticker, level)]。回傳 (最終名單 {ticker: level}, errors, notes)。
    strict=True：不修改提案，違規逐條列在 errors。strict=False：修正成合規（回測代理用），修正記在 notes。"""
    errs: list[str] = []
    notes: list[str] = []
    prop = dict(proposal)
    initial = not held
    stop = {t for t in stop_loss_names(held, p) if t not in new_positive}
    locked = {t for t, h in held.items() if h.days_held < p.min_hold_days and t not in stop}
    if eligible is not None:
        bad = [t for t in prop if t not in eligible and t not in held]
        if bad and strict:
            errs.append(f"{bad} 決策日無價格或流動性不足（20 日均成交值 < {p.min_adv / 1e8:.0f} 億），不可新增")
        for t in bad:
            prop.pop(t, None)

    if strict:
        if len(prop) != len(proposal):
            errs.append("picks 有重複的股票")
        for t in stop & set(prop):
            errs.append(f"{t} 相對成本下跌 {-held[t].ret:.1%} > {p.stop_loss:.0%}，觸發停損且今日沒有新的正面事件，必須移出名單")
        for t in locked:
            if t not in prop:
                errs.append(f"{t} 只持有 {held[t].days_held} 個交易日（< {p.min_hold_days}），未觸發停損前不可賣出")
            elif held[t].level == "high" and prop[t] == "medium":
                errs.append(f"{t} 只持有 {held[t].days_held} 個交易日（< {p.min_hold_days}），不可降級（降級 = 減碼）")
        added = [t for t in prop if t not in held]
        removed = [t for t in held if t not in prop and t not in stop]
        if blackout:
            if added:
                errs.append(f"今日為高波動事件前一交易日，不可新增部位：{added}")
            ups = [t for t in prop if t in held and held[t].level == "medium" and prop[t] == "high"]
            if ups:
                errs.append(f"今日為高波動事件前一交易日，不可升級（加碼）：{ups}")
        elif not initial and len(added) > p.max_swaps:
            errs.append(f"今日新增 {len(added)} 檔，超過每日換股上限 {p.max_swaps} 檔")
        if not initial and len(removed) > p.max_swaps:
            errs.append(f"今日主動移出 {len(removed)} 檔，超過每日換股上限 {p.max_swaps} 檔")
        n_min = p.n_conv_min
        if blackout:
            n_min = min(p.n_conv_min, len([t for t in held if t not in stop]))
        if not n_min <= len(prop) <= p.n_conv_max:
            errs.append(f"高信心部位需 {n_min}–{p.n_conv_max} 檔，你給了 {len(prop)} 檔")
        return prop, errs, notes

    # ---- 修正模式（回測代理）
    final: dict[str, str] = {}
    for t in held:
        if t in stop:
            notes.append(f"stop_loss:{t}")
            continue
        if t in locked or t in prop or blackout:                 # 事件前一日：凍結（不主動移出）
            lv = prop.get(t, held[t].level)
            if t in locked and held[t].level == "high" and lv == "medium":
                lv = "high"
            if blackout and held[t].level == "medium" and lv == "high":
                lv = "medium"
            final[t] = lv
    # 主動移出：held 中不在提案也未鎖定者；超過上限的保留（優先保留提案順位中較前者——不在提案者無順位，依原順序）
    drop = [t for t in held if t not in final and t not in stop]
    if not initial and len(drop) > p.max_swaps:
        for t in drop[p.max_swaps:]:
            final[t] = held[t].level
            notes.append(f"swap_limit_kept:{t}")
    target_n = max(len(proposal), p.n_conv_min) if proposal else p.n_conv_min
    target_n = min(target_n, p.n_conv_max)
    adds = 0
    for t, lv in proposal:
        if t in final or t in held or t not in prop:
            continue
        if len(final) >= target_n:
            break
        if blackout:
            notes.append("blackout_no_new")
            break
        if not initial and adds >= p.max_swaps:
            notes.append("swap_limit_reached")
            break
        final[t] = lv
        adds += 1
    while len(final) > p.n_conv_max:                       # 鎖定＋提案超過 12 檔：去掉順位最後、未鎖定者
        cand = [t for t in reversed(list(final)) if t not in locked]
        if not cand:
            break
        final.pop(cand[0])
    return final, errs, notes


# --------------------------------------------------------------------------- 高波動事件

def load_event_calendar(path: Path | None = None) -> list[dict]:
    p = path or EVENT_CALENDAR_PATH
    if not p.exists():
        return []
    return [e for e in json.loads(p.read_text(encoding="utf-8"))["events"] if e.get("date")]


def load_stock_events(path: Path | None = None, kind: str | None = None) -> list[dict]:
    """event_calendar.json 的個股事件（法說會等）：[{date, kind, ticker, name, url}]。"""
    p = path or EVENT_CALENDAR_PATH
    if not p.exists():
        return []
    evs = json.loads(p.read_text(encoding="utf-8")).get("stock_events", [])
    return [e for e in evs if e.get("date") and e.get("ticker") and (kind is None or e.get("kind") == kind)]


def _next_weekday(d: date) -> date:
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def blackout_events(trade_date: str, events: list[dict] | None = None, next_session: str | None = None) -> list[dict]:
    """trade_date 是否為某高波動事件「前一交易日」：事件的美國日期 E 滿足 trade_date ≤ E < 下一個交易日
    （美國事件發生在台北時間夜間，交易日當天收盤後才揭曉）。next_session 未知時以下一個平日近似。"""
    evs = load_event_calendar() if events is None else events
    t = date.fromisoformat(trade_date)
    nxt = date.fromisoformat(next_session) if next_session else _next_weekday(t)
    return [e for e in evs if is_blackout_event(e) and t <= date.fromisoformat(e["date"]) < nxt]


# 程式強制凍結（不新增、不升級）的事件種類；其他美國事件（CPI、PCE、非農、其他科技巨頭財報）只在提示詞要求「降低新建倉」
BLACKOUT_KINDS = frozenset({"fomc", "nvidia_earnings", "us_election"})


def is_blackout_event(e: dict) -> bool:
    return bool(e.get("blackout", e.get("kind") in BLACKOUT_KINDS))


def us_event_window(trade_date: str, events: list[dict] | None = None,
                    next_session: str | None = None) -> list[tuple[dict, str]]:
    """美國事件的「前 1 日」與「當日」（以台股交易日計；美國事件在台北時間夜間揭曉，該台股交易日收盤前尚未公布）：
      當日   = 事件美國日期 E 所在的台股交易日：trade_date ≤ E < 下一交易日
      前 1 日 = 再前一個交易日：下一交易日 ≤ E < 下下個交易日
    回傳 [(事件, "當日"|"前1日")]，依日期排序。next_session 未知時以平日近似。"""
    evs = load_event_calendar() if events is None else events
    t = date.fromisoformat(trade_date)
    n1 = date.fromisoformat(next_session) if next_session else _next_weekday(t)
    n2 = _next_weekday(n1)
    out = []
    for e in sorted(evs, key=lambda x: x["date"]):
        d = date.fromisoformat(e["date"])
        if t <= d < n1:
            out.append((e, "當日"))
        elif n1 <= d < n2:
            out.append((e, "前1日"))
    return out


# --------------------------------------------------------------------------- 成本與報酬

def pick_return(panel, ticker: str, entry_date: str, D: str) -> float | None:
    """還原價相對成本的報酬：成本 = 進場交易日的成交均價（換算成還原價口徑）。entry_date 尚未收盤（> D）或無資料 → None。"""
    if entry_date > D or entry_date not in panel.avg_price.index:
        return None
    avg = panel.avg_price.at[entry_date, ticker]
    close = panel.close.at[entry_date, ticker]
    adj = panel.adj_close.at[entry_date, ticker]
    px = panel.adj_close[ticker].loc[:D].dropna()
    if any(pd.isna(x) or not x for x in (avg, close, adj)) or px.empty:
        return None
    return float(px.iloc[-1] / (avg * adj / close) - 1)
