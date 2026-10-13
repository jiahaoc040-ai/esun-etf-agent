"""自行記帳（競賽系統不提供前日淨值/持股的 API，需自己追蹤，並每天對帳主辦方後台）。

規則：委託全數以「當日成交均價」成交（均價 = 成交金額 ÷ 成交股數），
買賣各收 0.1425% 手續費、賣出另收 0.3% 證交稅，從現金扣除。
NAV = Σ 持股股數 × 當日收盤價 + 現金。
公司行動（1005 版辦法六(十二)註 1）：
  - 除息現金、除權配發股數**都不在除權息日入帳**，由主辦方統一累積、初賽結束時一併加回最終淨值。
    因此本 Ledger 在除權息日**不增加現金、不增加股數**（刻意沒有 ex-rights／ex-dividend 的入帳方法）；
    除權息日股價下調但股數不變，日 NAV 會偏低，期末才補，屬官方設計。
  - 股票分割／面額變更／減資（單日股價比 ≥ 約 1.67 倍的跳空）由主辦方依實際情況調整持股數量，用 apply_split 處理。
  - 每日以主辦方後台庫存為準：reconcile 比對、overwrite_from_official 覆寫。賣出股數上限也以官方庫存為準（C13）。
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import FEE_RATE, INITIAL_CAPITAL, TAX_RATE


@dataclass
class Ledger:
    cash: float = float(INITIAL_CAPITAL)
    holdings: dict[str, int] = field(default_factory=dict)     # ticker -> 股數
    as_of: str | None = None                                    # 最後結算日 YYYY-MM-DD
    fee_rate: float = FEE_RATE                                  # 預設為官方費率；歸因分析可設 0 做「免費用」對照
    tax_rate: float = TAX_RATE

    # ---- 成交 ----
    def apply_fills(self, orders: list[dict], avg_price: dict[str, float]) -> dict:
        """依當日均價結算委託，回傳費用明細。賣出先算，以免同日換股時現金暫時為負的誤判。"""
        fee_total = tax_total = 0.0
        for o in sorted(orders, key=lambda o: o["side"] != "SELL"):
            t, n = o["ticker"], o["shares"]
            amt = n * avg_price[t]
            fee = amt * self.fee_rate
            if o["side"] == "SELL":
                if n > self.holdings.get(t, 0):
                    raise ValueError(f"超賣 {t}: 賣 {n} > 持有 {self.holdings.get(t, 0)}")
                tax = amt * self.tax_rate
                self.cash += amt - fee - tax
                self.holdings[t] -= n
                if self.holdings[t] == 0:
                    del self.holdings[t]
                tax_total += tax
            else:
                self.cash -= amt + fee
                self.holdings[t] = self.holdings.get(t, 0) + n
            fee_total += fee
        return {"fee": fee_total, "tax": tax_total}

    # ---- 公司行動
    def apply_split(self, ticker: str, ratio: float) -> int:
        """股票分割／面額變更（ratio > 1，1 股變 ratio 股）或減資（ratio < 1）後的持股調整，回傳新股數。
        注意：**除權配股不走這裡**——配發股數期末才由主辦方一次加回，除權日不得增加股數。"""
        if ticker in self.holdings:
            self.holdings[ticker] = int(round(self.holdings[ticker] * ratio))
        return self.holdings.get(ticker, 0)

    # ---- 評價 ----
    def market_value(self, close: dict[str, float]) -> float:
        return sum(n * close[t] for t, n in self.holdings.items())

    def nav(self, close: dict[str, float]) -> float:
        return self.market_value(close) + self.cash

    def weights(self, close: dict[str, float]) -> dict[str, float]:
        nav = self.nav(close)
        return {t: n * close[t] / nav for t, n in self.holdings.items()}

    def cash_ratio(self, close: dict[str, float]) -> float:
        return self.cash / self.nav(close)

    # ---- 對帳 ----
    def reconcile(self, official_holdings: dict[str, int], official_cash: float | None = None,
                  cash_tol: float = 1.0) -> list[str]:
        """與主辦方後台庫存比對；回傳差異清單。有差異時應以官方為準（overwrite_from_official）。"""
        diffs = []
        for t in sorted(set(self.holdings) | set(official_holdings)):
            mine, theirs = self.holdings.get(t, 0), official_holdings.get(t, 0)
            if mine != theirs:
                diffs.append(f"{t}: 自記 {mine} ≠ 官方 {theirs}")
        if official_cash is not None and abs(self.cash - official_cash) > cash_tol:
            diffs.append(f"cash: 自記 {self.cash:,.2f} ≠ 官方 {official_cash:,.2f}")
        return diffs

    def overwrite_from_official(self, official_holdings: dict[str, int], official_cash: float | None = None):
        self.holdings = {t: n for t, n in official_holdings.items() if n}
        if official_cash is not None:
            self.cash = float(official_cash)

    # ---- 存讀 ----
    def save(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(
            {"as_of": self.as_of, "cash": self.cash, "holdings": self.holdings},
            ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Ledger":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(cash=d["cash"], holdings={k: int(v) for k, v in d["holdings"].items()}, as_of=d.get("as_of"))
