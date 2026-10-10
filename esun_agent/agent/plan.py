"""目標權重 → decisions／no_trade_decisions／orders，以及 posture、funding_for。

全部是機械運算，LLM 不參與：
  - 目標股數與委託股數用 esun_agent.orders（官方公式），權重先四捨五入到 6 位再算，確保 D-Plan 上的 target_weight 與委託一致。
  - 委託後現金比（前日收盤價估算）必須 ≥ MIN_CASH_AFTER（2%）：成交用當日均價，買進最多可能貴 10%；不足時
    按比例縮減買進（二分搜尋最大的縮放係數），賣出不動。
  - posture 由實際委託推得：|買−賣| ≤ 2% 淨值 → hold，淨買超 → increase，淨賣超 → reduce；現金區間以估算現金比 ±2%
    （夾在 1%–22% 緩衝內，且一定包含估算值）。
  - funding_for 只在「缺口真的存在」時標記（C14）：拿掉被標記的調度賣出後，現金比會低於宣告區間下限。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import CASH_BUFFER_HIGH, CASH_BUFFER_LOW, HOLD_TOLERANCE
from ..orders import derive_orders, estimate_flows, target_shares

MIN_CASH_AFTER = 0.02


@dataclass
class Plan:
    decisions: list[dict] = field(default_factory=list)       # decision_id 尚未編號：{ticker, action, target_weight}
    no_trade: list[str] = field(default_factory=list)
    orders: list = field(default_factory=list)                # orders.Order
    cash_after: float = 0.0                                   # 前日收盤價估算的委託後現金（元）
    buy: float = 0.0
    sell: float = 0.0
    scale: float = 1.0                                        # 買進縮減係數（1 = 未縮減）
    notes: list[str] = field(default_factory=list)


def _build(target: dict[str, float], nav: float, close: dict[str, float], holdings: dict[str, int]):
    decisions, no_trade = [], []
    for t in sorted(set(holdings) | set(target)):
        held = holdings.get(t, 0)
        if t not in close:
            if held:
                no_trade.append(t)
            continue
        w_exact = max(0.0, target.get(t, 0.0))
        tgt0 = target_shares(w_exact, nav, close[t]) if w_exact > 0 else 0
        if tgt0 == held:                       # 用未四捨五入的權重判斷「沒有異動」（鎖在現權重的持股剛好等於現有股數）
            if held:
                no_trade.append(t)
            continue
        w = round(w_exact, 6)                  # 有異動者才寫進 D-Plan，委託以四捨五入後的 target_weight 為準
        tgt = target_shares(w, nav, close[t]) if w > 0 else 0
        delta = tgt - held
        if delta == 0:
            if held:
                no_trade.append(t)
            continue
        action = "BUY" if held == 0 else "SELL_ALL" if tgt == 0 else "ADD" if delta > 0 else "TRIM"
        decisions.append({"ticker": t, "action": action, "target_weight": 0.0 if tgt == 0 else w})
    return decisions, no_trade


def _orders(decisions: list[dict], nav, close, holdings):
    ds = [{**d, "decision_id": f"D{i}"} for i, d in enumerate(decisions, 1)]
    return derive_orders(ds, nav, close, holdings)


def plan_orders(target: dict[str, float], nav: float, cash: float, close: dict[str, float],
                holdings: dict[str, int], min_cash_after: float = MIN_CASH_AFTER) -> Plan:
    """target = 目標權重（占 NAV）。回傳 Plan（decisions 尚未編號）。"""
    cur = {t: n * close[t] / nav for t, n in holdings.items() if t in close}

    def scaled(k: float) -> dict[str, float]:
        out = dict(target)
        for t in set(target) | set(cur):
            tw, cw = target.get(t, 0.0), cur.get(t, 0.0)
            if tw > cw:
                out[t] = cw + k * (tw - cw)
        return out

    def attempt(k):
        decs, nt = _build(scaled(k), nav, close, holdings)
        ords = _orders(decs, nav, close, holdings)
        buy, sell = estimate_flows([o.to_dict() for o in ords], close)
        return decs, nt, ords, buy, sell, cash - (buy - sell)

    decs, nt, ords, buy, sell, cash_after = attempt(1.0)
    plan = Plan(decs, nt, ords, cash_after, buy, sell, 1.0)
    if cash_after / nav >= min_cash_after:
        return plan
    lo, hi = 0.0, 1.0
    best = attempt(0.0)
    if best[5] / nav >= min_cash_after:
        for _ in range(30):
            mid = (lo + hi) / 2
            r = attempt(mid)
            if r[5] / nav >= min_cash_after:
                lo, best = mid, r
            else:
                hi = mid
        k = lo
    else:
        k = 0.0                         # 即使完全不買也低於 2%（現金本來就低）：不買，其餘照舊
    decs, nt, ords, buy, sell, cash_after = best
    return Plan(decs, nt, ords, cash_after, buy, sell, k,
                [f"buys_scaled:{k:.3f}（委託後現金比原為 {(cash - (plan.buy - plan.sell)) / nav:.2%} < {min_cash_after:.0%}）"])


def posture_for(plan: Plan, nav: float) -> dict:
    net = plan.buy - plan.sell
    if abs(net) <= HOLD_TOLERANCE * nav * 0.999:
        intent = "hold"
    else:
        intent = "increase" if net > 0 else "reduce"
    if intent == "increase" and not net > 0:
        intent = "hold"
    c = plan.cash_after / nav
    lo = min(max(CASH_BUFFER_LOW, c - 0.02), c)
    hi = max(min(CASH_BUFFER_HIGH, c + 0.02), c)
    return {"net_exposure_intent": intent,
            "target_cash_pct_range": [math.floor(lo * 1e4) / 1e4, math.ceil(hi * 1e4) / 1e4]}


def assign_funding(decisions: list[dict], plan: Plan, nav: float, close: dict[str, float], lo: float) -> None:
    """就地在 decisions（已含 decision_id）上標記 funding_for。只在缺口真的存在時標記：
    被標記賣出的總金額 > 估算現金比 − 區間下限，使得「拿掉它們後現金比 < 下限」。"""
    c = plan.cash_after / nav
    buys = [d for d in decisions if d["action"] in ("BUY", "ADD")]
    sells = [d for d in decisions if d["action"] in ("TRIM", "SELL_ALL")]
    if not buys or not sells:
        return
    proceeds = {}
    for o in plan.orders:
        if o.side == "SELL":
            proceeds[o.ticker] = o.shares * close[o.ticker] / nav
    need = c - lo
    chosen, tot = [], 0.0
    for d in sorted(sells, key=lambda d: -proceeds.get(d["ticker"], 0.0)):
        if tot > need + 1e-9:
            break
        if proceeds.get(d["ticker"], 0.0) > 0:
            chosen.append(d)
            tot += proceeds[d["ticker"]]
    if tot <= need + 1e-9:
        return                                            # 缺口不存在 → 不標記
    buy_amt = {o.ticker: o.shares * close[o.ticker] for o in plan.orders if o.side == "BUY"}
    targets = [d["decision_id"] for d in sorted(buys, key=lambda d: -buy_amt.get(d["ticker"], 0.0))][:5]
    for d in chosen:
        d["funding_for"] = list(targets)
