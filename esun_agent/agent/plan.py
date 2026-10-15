"""目標權重 → decisions／no_trade_decisions／orders，以及 posture、funding_for。

全部是機械運算，LLM 不參與：
  - 目標股數與委託股數用 esun_agent.orders（官方公式），權重先四捨五入到 6 位再算，確保 D-Plan 上的 target_weight 與委託一致。
  - 委託後現金比（前日收盤價估算）必須 ≥ MIN_CASH_AFTER（2%）：成交用當日均價，買進最多可能貴 10%；不足時
    按比例縮減買進（二分搜尋最大的縮放係數），賣出不動。
  - posture 由實際委託推得：|買−賣| ≤ 2% 淨值 → hold，淨買超 → increase，淨賣超 → reduce；現金區間以估算現金比 ±2%
    （夾在 1%–22% 緩衝內，且一定包含估算值）。
  - funding_for 只在「缺口真的存在」時標記（C14）：拿掉被標記的調度賣出後，現金比會低於宣告區間下限。
  - 合規保險：
      被動超限（force_passive_trims）：前日結算權重（nav_tradable、T-1 收盤）> 9.5%（2330 > 24%），或資料包合規提示
      點名的個股 → 強制 TRIM 回到目標權重（不受無交易帶影響、不會被延後）。
      漲停壓力測試（stress_cap_buys）：BUY/ADD 的目標權重以 T-1 收盤 × 1.10 計仍須 ≤ 10%（2330 ≤ 25%），否則自動降權。
  - action：前日結算 shares 為 0 才是 BUY，否則 ADD（receivable_shares 不算持股）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import CASH_BUFFER_HIGH, CASH_BUFFER_LOW, HOLD_TOLERANCE, passive_soft_limit, stress_cap
from ..orders import derive_orders, estimate_flows, target_shares

MIN_CASH_AFTER = 0.02
MAX_DECISIONS = 60                # schema v4.3：decisions、orders 各最多 60 筆（v4.2 為 30）
STRESS_EPS = 1e-6                 # target_weight 會四捨五入到 6 位：壓力測試上限再減一點，避免進位後超過
BUY_BUFFER = 0.03                 # 集中策略：每買進 NAV 的 10%，委託後現金比多留 0.3%（均價可能高於前日收盤）


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


def passive_overweight(cur_w: dict[str, float]) -> dict[str, float]:
    """前日結算權重 > 9.5%（2330 > 24%）的持股。"""
    return {t: w for t, w in cur_w.items() if w > passive_soft_limit(t)}


def force_passive_trims(target: dict[str, float], strategy: dict[str, float], cur_w: dict[str, float],
                        forced: set[str], cap) -> tuple[dict[str, float], dict[str, float]]:
    """forced 中的持股強制 TRIM 回到目標權重：min(策略目標, 內部單檔上限 cap(t))，且一定低於現權重。
    （無交易帶或凍結可能把目標鎖在現權重，這裡覆寫。）回傳 (新目標, {代號: 新目標權重})。"""
    out, done = dict(target), {}
    for t in sorted(forced):
        if t not in cur_w:
            continue
        w = min(strategy.get(t, 0.0), out.get(t, 0.0), cap(t))
        if w >= cur_w[t]:
            w = min(cap(t), cur_w[t] * 0.9)
        out[t] = w
        done[t] = w
    return out, done


def stress_cap_buys(target: dict[str, float], cur_w: dict[str, float]) -> tuple[dict[str, float], list[str]]:
    """BUY/ADD（目標 > 現權重）的目標權重：漲停 ×1.10 後仍須 ≤ 個股上限 → 超過者自動降權。"""
    out, notes = dict(target), []
    for t, w in target.items():
        cap = stress_cap(t) - STRESS_EPS
        if w > cur_w.get(t, 0.0) and w > cap:
            out[t] = max(cap, 0.0)
            notes.append(f"stress_capped:{t} {w:.4f}→{out[t]:.4f}")
    return out, notes


def limit_decisions(target: dict[str, float], nav: float, close: dict[str, float], holdings: dict[str, int],
                    max_n: int = MAX_DECISIONS, protect: set[str] | frozenset = frozenset()) -> tuple[dict[str, float], list[str]]:
    """一天的異動超過 max_n 檔（例如合規基準 A ↔ 集中組合的整體轉換）時，把最不重要的異動延到隔天：
    先讓續抱者的小幅加減碼維持現權重（差距最小者優先），再延後權重最小的新建倉，最後延後出清（維持現權重）。
    protect（被動超限的強制減碼）不會被延後。"""
    cur = {t: n * close[t] / nav for t, n in holdings.items() if t in close}
    t = dict(target)
    notes: list[str] = []
    for _ in range(200):
        decs, _ = _build(t, nav, close, holdings)
        if len(decs) <= max_n:
            break
        cont = sorted((d for d in decs if d["action"] in ("ADD", "TRIM") and d["ticker"] not in protect),
                      key=lambda d: (abs(d["target_weight"] - cur.get(d["ticker"], 0.0)), d["ticker"]))
        buys = sorted((d for d in decs if d["action"] == "BUY"), key=lambda d: (d["target_weight"], d["ticker"]))
        sells = sorted((d for d in decs if d["action"] == "SELL_ALL" and d["ticker"] not in protect),
                       key=lambda d: (cur.get(d["ticker"], 0.0), d["ticker"]))
        if cont:
            x = cont[0]["ticker"]
            t[x] = cur[x]
        elif buys:
            x = buys[0]["ticker"]
            t.pop(x)
        elif sells:
            x = sells[0]["ticker"]
            t[x] = cur[x]
        else:
            break
        notes.append(f"deferred:{x}")
    if notes:
        notes = [f"decisions_limited:{len(notes)} 檔延到隔日（單日最多 {max_n} 筆）"] + notes
    return t, notes


def _orders(decisions: list[dict], nav, close, holdings):
    ds = [{**d, "decision_id": f"D{i}"} for i, d in enumerate(decisions, 1)]
    return derive_orders(ds, nav, close, holdings)


def plan_orders(target: dict[str, float], nav: float, cash: float, close: dict[str, float],
                holdings: dict[str, int], min_cash_after: float = MIN_CASH_AFTER, buy_buffer: float = 0.0,
                protect: set[str] | frozenset = frozenset()) -> Plan:
    """target = 目標權重（占 NAV）。回傳 Plan（decisions 尚未編號）。BUY/ADD 先過漲停壓力測試（stress_cap_buys）；
    異動超過 60 檔時以 limit_decisions 延後一部分（protect 不延後）。
    委託後估算現金比需 ≥ min_cash_after + buy_buffer × 買進金額／NAV（買進用 T 日均價成交，可能比前日收盤貴；
    換股多的日子買得多，緩衝也要大）。"""
    cur = {t: n * close[t] / nav for t, n in holdings.items() if t in close}
    target, stress_notes = stress_cap_buys(target, cur)
    target, lim_notes = limit_decisions(target, nav, close, holdings, protect=protect)
    lim_notes = stress_notes + lim_notes

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

    def ok(r) -> bool:
        return r[5] / nav >= min_cash_after + buy_buffer * r[3] / nav

    first = attempt(1.0)
    decs, nt, ords, buy, sell, cash_after = first
    plan = Plan(decs, nt, ords, cash_after, buy, sell, 1.0)
    plan.notes = list(lim_notes)
    if ok(first):
        return plan
    floor = min_cash_after + buy_buffer * buy / nav
    lo, hi = 0.0, 1.0
    best = attempt(0.0)
    if ok(best):
        for _ in range(30):
            mid = (lo + hi) / 2
            r = attempt(mid)
            if ok(r):
                lo, best = mid, r
            else:
                hi = mid
        k = lo
    else:
        k = 0.0                         # 即使完全不買也低於 2%（現金本來就低）：不買，其餘照舊
    decs, nt, ords, buy, sell, cash_after = best
    return Plan(decs, nt, ords, cash_after, buy, sell, k, lim_notes +
                [f"buys_scaled:{k:.3f}（委託後現金比原為 {(cash - (plan.buy - plan.sell)) / nav:.2%} < {floor:.2%}）"])


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
