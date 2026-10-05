"""官方委託推導演算法（D-Plan 指南 ⑥，schema C2 稱 derive-orders.v2）。

    目標股數 = floor( target_weight × 前日淨值 ÷ 前日收盤價 ÷ 1000 ) × 1000
    委託股數 = 目標股數 − 目前持有股數     （正=BUY，負=SELL）

orders 必須「逐位元等於」此推導結果，不可手調。
持有股數必須來自主辦方後台的結算庫存，不可自行推算（否則可能超賣 → C13 整份拒收）。
"""
import math
from dataclasses import dataclass

from .config import LOT_SIZE


@dataclass(frozen=True)
class Order:
    ticker: str
    side: str          # "BUY" | "SELL"
    shares: int
    decision_ref: str
    derivation_detail: str

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "side": self.side,
            "shares": self.shares,
            "decision_ref": self.decision_ref,
            "derivation_detail": self.derivation_detail,
        }


def target_shares(target_weight: float, prev_nav: float, prev_close: float) -> int:
    # 先四捨五入到 1e-9 再 floor，避免 0.23*102e6/1480/1000 這類浮點誤差少算一張
    lots = target_weight * prev_nav / prev_close / LOT_SIZE
    return math.floor(round(lots, 9)) * LOT_SIZE


def _fmt(x: float) -> str:
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,}"


def derive_order(decision: dict, prev_nav: float, prev_close: float, held_shares: int) -> Order | None:
    """單筆 decision → order；目標股數等於持股時回傳 None（不產生委託）。"""
    tw = decision["target_weight"]
    tgt = target_shares(tw, prev_nav, prev_close)
    delta = tgt - held_shares
    if delta == 0:
        return None
    side = "BUY" if delta > 0 else "SELL"
    detail = (
        f"target_shares = floor({tw}×{_fmt(prev_nav)} / {_fmt(prev_close)} / 1000)×1000 = {tgt:,}；"
        f"現持 {held_shares:,} → {side} {abs(delta):,}"
    )
    return Order(decision["ticker"], side, abs(delta), decision["decision_id"], detail[:300])


def derive_orders(decisions: list[dict], prev_nav: float, prev_close: dict[str, float],
                  holdings: dict[str, int]) -> list[Order]:
    out = []
    for d in decisions:
        o = derive_order(d, prev_nav, prev_close[d["ticker"]], holdings.get(d["ticker"], 0))
        if o is not None:
            out.append(o)
    return out


def estimate_flows(orders: list[dict], prev_close: dict[str, float]) -> tuple[float, float]:
    """以前日收盤價估算 (買進金額, 賣出金額)，不含費稅——與 server C12 重算口徑一致（由官方正/反例反推）。"""
    buy = sum(o["shares"] * prev_close[o["ticker"]] for o in orders if o["side"] == "BUY")
    sell = sum(o["shares"] * prev_close[o["ticker"]] for o in orders if o["side"] == "SELL")
    return buy, sell
