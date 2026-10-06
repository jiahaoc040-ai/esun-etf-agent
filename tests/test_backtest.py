import pandas as pd
import pytest

from esun_agent import backtest as bt
from esun_agent.ledger import Ledger
from esun_agent.orders import derive_orders
from esun_agent.strategy.factors import compute_factors
from esun_agent.strategy.portfolio import PortfolioParams

from .helpers_market import make_panel

STRAT = bt.Strategy("t", {"mom20": 0.5, "mom60": 0.5}, PortfolioParams(), min_adv=0.0)


def run(panel, **kw):
    return bt.run_backtest(panel, STRAT, compute_factors(panel), None, **kw)


def test_perf():
    nav = pd.Series([100.0, 110.0, 99.0, 108.9], index=list("abcd"))
    p = bt.perf(nav)
    assert p["total_return"] == pytest.approx(0.089) and p["mdd"] == pytest.approx(-0.1) and p["days"] == 3


def test_run_invariants_and_no_violations():
    panel, dates, _ = make_panel(seed=4)
    r = run(panel)
    d = r.daily
    assert d.index[0] == dates[61] and r.nav.index[0] == dates[60] and r.nav.iloc[0] == 1_000_000_000
    assert (d["n_hold"] == 25).all() and r.violations == {"holdings": 0, "weight": 0, "cash": 0}
    assert d["cash_ratio"].between(0, 0.25).all() and d["max_w"].max() < 0.25
    assert (d["fee"] >= 0).all() and d["tax"].sum() > 0 and d["turnover"].iloc[0] > 0.9 * 0.5  # 第一天建倉
    assert r.unfilled == 0 and r.rejected_buys == 0


def test_nav_matches_ledger_recompute():
    panel, dates, _ = make_panel(seed=5, n_days=75)
    r = run(panel)
    # 第一天：全部現金 → 以均價買進、收盤估值；只有手續費成本
    T = dates[61]
    assert r.nav[T] < 1e9 * 1.2 and r.daily.loc[T, "fee"] == pytest.approx(
        r.daily.loc[T, "turnover"] * 2 * 1e9 * 0.001425, rel=0.02)


def test_no_lookahead_future_data_does_not_change_past():
    panel, dates, _ = make_panel(seed=6)
    base = run(panel)
    E = dates[90]
    panel2, _, _ = make_panel(seed=6)
    for name in ("close", "open", "avg_price", "volume", "value", "foreign_net", "trust_net", "adj_close"):
        getattr(panel2, name).loc[dates[91]:] *= 3.7
    other = run(panel2)
    pd.testing.assert_series_equal(base.nav[:E], other.nav[:E])


def test_decision_on_T_uses_only_data_through_D(monkeypatch):
    """改掉 T 日的收盤、量額、法人（保留 T 日均價作為成交價）→ T 日委託完全不變。"""
    seen = []
    orig = bt.derive_orders

    def spy(decisions, nav, prev_close, holdings):
        out = orig(decisions, nav, prev_close, holdings)
        seen.append([(o.ticker, o.side, o.shares) for o in out])
        return out
    monkeypatch.setattr(bt, "derive_orders", spy)
    panel, dates, _ = make_panel(seed=7, n_days=100)
    run(panel, end=dates[75])
    a = list(seen)
    seen.clear()
    panel2, _, _ = make_panel(seed=7, n_days=100)
    T = dates[75]
    for name in ("close", "open", "volume", "value", "foreign_net", "trust_net"):
        getattr(panel2, name).loc[T] *= 0.5
    panel2.adj_close.loc[T] *= 0.5
    run(panel2, end=T)
    assert a == seen and len(a[-1]) >= 0


def test_split_on_held_stock_keeps_nav_continuous():
    # 全員 4:1 分割在第 70 天 → 持股股數 ×4，NAV 不因分割而跳水
    splits = {(t, 70): 4 for t in ["2330"] + [str(1001 + i) for i in range(39)]}
    panel, dates, _ = make_panel(seed=8, splits=splits)
    assert len(panel.events) == 40
    r = run(panel)
    ret = r.nav.pct_change()
    assert ret.loc[dates[70]] > -0.15 and ret.abs().max() < 0.2
    assert r.violations == {"holdings": 0, "weight": 0, "cash": 0}


def test_reject_unaffordable_buys():
    fills = [{"ticker": "A", "side": "BUY", "shares": 1000}, {"ticker": "B", "side": "BUY", "shares": 3000},
             {"ticker": "C", "side": "SELL", "shares": 1000}]
    avg = {"A": 100.0, "B": 100.0, "C": 100.0}
    rej = bt._reject_unaffordable_buys(fills, avg, cash=50_000.0)    # 賣 C 入帳約 9.6 萬 + 5 萬 < 買 40 萬
    assert [o["ticker"] for o in rej] == ["B"]
    assert bt._reject_unaffordable_buys(fills, avg, cash=1e9) == []


def test_no_trade_names_generate_no_orders():
    """無交易帶：目標權重 = 現權重（以前日收盤計）時，derive_orders 不產生委託。"""
    holdings = {"1001": 123_000, "1002": 47_000}
    close = {"1001": 87.3, "1002": 1234.5}
    nav = 1e9
    decisions = [{"ticker": t, "target_weight": n * close[t] / nav, "decision_id": f"D{i}"}
                 for i, (t, n) in enumerate(holdings.items(), 1)]
    assert derive_orders(decisions, nav, close, holdings) == []


def test_window_returns_shape():
    panel, dates, _ = make_panel(seed=9)
    w = bt.window_returns(panel, STRAT, compute_factors(panel), None, length=10, step=10)
    assert list(w.columns) == ["start", "end", "ret", "mdd", "bench_ret", "viol"] and len(w) >= 4
    assert (w["viol"] == 0).all()
    for _, row in w.iterrows():
        assert dates.index(row["end"]) - dates.index(row["start"]) == 9
