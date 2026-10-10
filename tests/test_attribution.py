import numpy as np
import pandas as pd
import pytest

from esun_agent import attribution as at
from esun_agent import backtest as bt
from esun_agent.data.etf_holdings import check_target_weights
from esun_agent.ledger import Ledger
from esun_agent.strategy import factors as fa
from esun_agent.strategy import marketcap as mc
from esun_agent.strategy import portfolio as po
from esun_agent.strategy.compliance import ComplianceBenchmark, ComplianceParams
from esun_agent.strategy.core_satellite import CoreSatellite, CoreSatelliteParams

from .helpers_market import make_panel


def _setup(seed=21, n_days=130):
    panel, dates, tickers = make_panel(seed=seed, n_tickers=60, n_days=n_days)
    f = fa.compute_factors(panel)
    caps, _ = mc.market_cap(panel, shares=pd.Series(np.arange(1, 61) * 1e6, index=panel.close.columns))
    return panel, dates, f, caps


# ------------------------------------------------------------------ Ledger 費率
def test_ledger_zero_costs():
    L = Ledger(cash=1e6, fee_rate=0.0, tax_rate=0.0)
    out = L.apply_fills([{"ticker": "A", "side": "BUY", "shares": 1000}], {"A": 100.0})
    assert out == {"fee": 0.0, "tax": 0.0} and L.cash == pytest.approx(1e6 - 1e5)
    L.apply_fills([{"ticker": "A", "side": "SELL", "shares": 1000}], {"A": 100.0})
    assert L.cash == pytest.approx(1e6)
    default = Ledger(cash=1e6)
    default.apply_fills([{"ticker": "A", "side": "BUY", "shares": 1000}], {"A": 100.0})
    assert default.cash == pytest.approx(1e6 - 1e5 * 1.001425)


def test_cost_free_backtest_has_no_fees_and_higher_nav():
    panel, dates, f, caps = _setup()
    strat = CoreSatellite("t", CoreSatelliteParams(core_k=22, sat_n=6, min_adv=0.0))
    free = bt.run_backtest(panel, strat, f, None, caps=caps, cost_free=True)
    paid = bt.run_backtest(panel, strat, f, None, caps=caps)
    assert free.daily["fee"].sum() == 0 and free.daily["tax"].sum() == 0
    assert free.nav.iloc[-1] > paid.nav.iloc[-1]


# ------------------------------------------------------------------ 紙上組合與歸因
def test_p0_equals_cap_weight_benchmark():
    panel, dates, f, caps = _setup()
    nav, _ = at.paper_nav(panel, f, caps, at.BenchmarkDecider())
    b = bt.cap_weight_benchmark(panel, caps).loc[nav.index]
    np.testing.assert_allclose(nav.values / nav.iloc[0], (b / b.iloc[0]).values, rtol=1e-10)


def test_paper_nav_weights_drift_between_rebalances():
    panel, dates, f, caps = _setup(seed=22)
    s5 = CoreSatellite("t", CoreSatelliteParams(core_k=22, sat_n=6, min_adv=0.0, rebalance_every=5, band=0.0, cash_target=0.0))
    nav5, log = at.paper_nav(panel, f, caps, s5)
    assert len(log) == -(-(len(dates) - 61) // 5)                    # 只在第 1、6、11… 天決策
    nav1, _ = at.paper_nav(panel, f, caps, CoreSatellite("t", CoreSatelliteParams(core_k=22, sat_n=6, min_adv=0.0,
                                                                                  rebalance_every=1, band=0.0, cash_target=0.0)))
    first = dates[61]
    assert nav5[first] == pytest.approx(nav1[first])                  # 第一天建倉一樣
    assert not np.allclose(nav5.values, nav1.values)


def test_ladder_telescopes_exactly():
    panel, dates, f, caps = _setup(seed=23)
    prm = CoreSatelliteParams(core_k=22, sat_n=6, min_adv=0.0)
    A = at.attribution_ladder(panel, f, caps, None, prm, dates[100])
    codes = [r["code"] for r in A["rungs"]]
    assert codes == ["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7"]
    steps = [r["step"] for r in A["rungs"][1:]]
    assert sum(steps) == pytest.approx(A["total"])
    assert A["total"] == pytest.approx(A["rungs"][-1]["oos"] - A["rungs"][0]["oos"])
    assert A["rungs"][-1]["oos"] == pytest.approx(
        bt.run_backtest(panel, CoreSatellite("s", prm), f, None, caps=caps).summary(dates[100], None)["total_return"])
    assert A["rungs"][5]["step"] == pytest.approx(0.0)                # 沒有 top10 → Active Share 修正不改變任何東西
    assert set(A["sat"]) == {"days", "core", "sat", "pool_ew"} and A["sat"]["days"] == len(dates) - 100


def test_oos_return_base_is_last_nav_before_start():
    nav = pd.Series([1.0, 1.1, 1.21, 1.331], index=["a", "b", "c", "d"])
    assert at.oos_return(nav, "c") == pytest.approx(1.331 / 1.1 - 1)


# ------------------------------------------------------------------ 最小 Active Share 調整
def test_fit_active_share_minimal_touches_at_most_three_names():
    p = po.PortfolioParams(soft_cap=0.2, ap_margin=0.02)
    names = [f"S{i:02d}" for i in range(28)]
    w = {t: 0.97 / 28 + (0.04 if i < 3 else 0.0) - (0.04 * 3 / 25 if i >= 3 else 0.0) for i, t in enumerate(names)}
    top = sorted(w, key=lambda t: -w[t])[:10]
    etf = {"E": {t: w[t] for t in top}}                                 # 與我方前 10 大完全相同的 ETF
    assert not check_target_weights(w, etf, expected_etfs=["E"])["ok"]
    new, res = po.fit_active_share_minimal(w, etf, p)
    changed = [t for t in w if abs(new[t] - w[t]) > 1e-12]
    cut = [t for t in changed if new[t] < w[t]]
    assert 1 <= len(cut) <= 3 and sum(new.values()) == pytest.approx(sum(w.values()))
    order = sorted(w, key=lambda t: -w[t])
    got = [t for t in changed if new[t] > w[t]]
    assert all(t in order[10:28] for t in got)                            # 收下權重的只能是第 11–28 名
    assert all(new[t] >= 0.5 * w[t] - 1e-12 for t in cut)                 # 單檔最多降到一半
    assert po.fit_active_share_minimal(w, None, p) == (w, None)


# ------------------------------------------------------------------ 合規基準 A/B
def test_compliance_benchmark_structure_and_guard():
    panel, dates, f, caps = _setup(seed=24)
    D = dates[80]
    st = ComplianceBenchmark("A", ComplianceParams(min_adv=0.0))
    r = st.decide(fa.factors_on(f, D), caps.loc[D], {}, None)
    w = r.weights
    b = mc.capped_cap_weights(caps.loc[D].dropna())
    top = sorted(b, key=lambda t: (-b[t], t))[:28]
    assert set(w) == set(top) and sum(w.values()) == pytest.approx(0.97)     # (b) 前 28 檔、現金 3%
    assert max(w.values()) <= 0.20 + 1e-9 and max(v for t, v in w.items() if t != "2330") <= 0.08 + 1e-9
    assert st.force_rebalance({"2330": 0.25}, None) and st.force_rebalance({"1001": 0.096}, None)
    assert not st.force_rebalance({"2330": 0.15, "1001": 0.05}, None)
    r2 = bt.run_backtest(panel, st, f, None, caps=caps)
    assert r2.violations == {"holdings": 0, "weight": 0, "cash": 0} and (r2.daily["n_hold"] == 28).all()


def test_compliance_modes_run_with_top10():
    panel, dates, f, caps = _setup(seed=25)
    D = dates[80]
    base = ComplianceBenchmark("A", ComplianceParams(min_adv=0.0)).decide(fa.factors_on(f, D), caps.loc[D], {}, None).weights
    top = sorted(base, key=lambda t: -base[t])[:10]
    etf = {"E": {t: base[t] for t in top}}                              # 極端情況：ETF 前 10 大 = 我方前 10 大
    out = {}
    for mode in ("full", "minimal"):
        r = ComplianceBenchmark(mode, ComplianceParams(min_adv=0.0, ap_mode=mode)).decide(fa.factors_on(f, D), caps.loc[D], {}, etf)
        out[mode] = r
        assert sum(r.weights.values()) == pytest.approx(0.97)
    assert out["full"].active_share["ok"]                                # 全面壓低：通過
    mini = out["minimal"]
    cut = [t for t in base if mini.weights[t] < base[t] - 1e-9]
    assert 1 <= len(cut) <= 3                                           # 最小調整：只減 1–3 檔
    gain = [t for t in base if mini.weights[t] > base[t] + 1e-9]
    order = sorted(base, key=lambda t: -base[t])
    assert all(t in order[10:28] for t in gain)                         # 只加給第 11–28 名
    assert mini.active_share["min"] > 0                                 # 這個極端情況 1–3 檔不一定夠用（見報告），回傳檢查結果讓呼叫端判斷
