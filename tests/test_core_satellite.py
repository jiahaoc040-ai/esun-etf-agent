import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from esun_agent import backtest as bt
from esun_agent.data import shares as sh
from esun_agent.strategy import factors as fa
from esun_agent.strategy import marketcap as mc
from esun_agent.strategy.core_satellite import CoreSatellite, CoreSatelliteParams

from .helpers_market import make_panel

REAL = Path(__file__).parent / "fixtures" / "market" / "real"


# ------------------------------------------------------------------ 發行股數
def test_parse_tpex_shares_real_fixture():
    payload = json.loads((REAL / "otc_2025-01-02_EW_json.json").read_text(encoding="utf-8"))
    s = sh.parse_tpex_shares(payload)
    assert len(s) == 45 and s["1785"] == 596156243 and all(v > 0 for v in s.values())
    assert "00679B" not in s                                   # 不在 150 檔名單
    bad = {"tables": [{"fields": ["代號", "名稱"], "data": [["1785", "x"]]}]}
    with pytest.raises(KeyError):
        sh.parse_tpex_shares(bad)


def test_parse_twse_basic():
    uni = {"2330": {"name": "台積電", "market": "TWSE"}, "3443": {"name": "創意", "market": "TPEX"}}
    recs = [{"公司代號": "2330", "公司簡稱": "台積電", "已發行普通股數或TDR原發行股數": "25,932,000,000"},
            {"公司代號": "3443", "已發行普通股數或TDR原發行股數": "1"},      # 上櫃不取自此端點
            {"公司代號": "9999", "已發行普通股數或TDR原發行股數": "1"}]
    assert sh.parse_twse_basic(recs, uni) == {"2330": 25_932_000_000}
    with pytest.raises(KeyError):
        sh.parse_twse_basic([{"公司代號": "2330"}], uni)


def test_load_shares_coverage(tmp_path):
    p = tmp_path / "s.csv"
    assert sh.load_shares(p) is None                            # 不存在
    pd.DataFrame({"ticker": ["2330"], "shares": [1], "as_of": ["x"], "source": ["y"]}).to_csv(p, index=False)
    assert sh.load_shares(p) is None                            # 涵蓋率不足 → 回退代理
    from esun_agent.universe import load_universe
    df = pd.DataFrame({"ticker": list(load_universe()), "shares": 1000, "as_of": "x", "source": "y"})
    sh.save_shares(df, p)
    s = sh.load_shares(p)
    assert len(s) == 150 and s["2330"] == 1000


# ------------------------------------------------------------------ 市值與上限權重
def test_capped_cap_weights():
    cap = pd.Series({"2330": 1000.0, "a": 100.0, "b": 100.0, "c": 50.0, **{f"x{i}": 1.0 for i in range(20)}})
    w = mc.capped_cap_weights(cap)
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["2330"] == pytest.approx(0.20) and max(v for t, v in w.items() if t != "2330") <= 0.08 + 1e-12
    assert w["a"] == pytest.approx(0.08) and w["c"] > w["x0"]       # 超額分給其餘者
    w2 = mc.capped_cap_weights(pd.Series({"2330": np.nan, "a": 1.0, "b": 1.0}), budget=0.1)   # NaN 市值（無價格）不納入
    assert set(w2) == {"a", "b"} and sum(w2.values()) == pytest.approx(0.1)


def test_market_cap_sources():
    panel, dates, tickers = make_panel(seed=11)
    cap, src = mc.market_cap(panel, shares=None) if mc.load_shares() is None else (None, "issued_shares")
    if src == "adv60_proxy":
        assert cap.loc[dates[80], "1002"] == pytest.approx(panel.value["1002"].loc[dates[21]:dates[80]].mean())
    shares = pd.Series({t: 1000.0 for t in tickers})
    cap2, src2 = mc.market_cap(panel, shares=shares)
    assert src2 == "issued_shares"
    assert cap2.loc[dates[10], "1003"] == pytest.approx(panel.adj_close.loc[dates[10], "1003"] * 1000)


def test_market_cap_split_adjusted():
    panel, dates, _ = make_panel(seed=12, splits={("1005", 70): 4})
    cap, _ = mc.market_cap(panel, shares=pd.Series(1000.0, index=panel.close.columns))
    jump = cap["1005"].pct_change().loc[dates[70]]
    assert abs(jump) < 0.2                                      # 還原價 × 固定股數：分割不造成市值跳空


# ------------------------------------------------------------------ 新因子
def test_new_factors_values_and_no_lookahead():
    panel, dates, tickers = make_panel(seed=13)
    f = fa.compute_factors(panel)
    d, t = dates[90], tickers[4]
    px = panel.adj_close[t]
    assert f["mom20s5"].loc[d, t] == pytest.approx(px.loc[dates[85]] / px.loc[dates[65]] - 1)
    w = slice(dates[81], d)
    amt = (panel.trust_net[t].loc[w] * panel.avg_price[t].loc[w]).sum() / panel.value[t].loc[w].sum()
    assert f["inst10"].loc[d, t] == pytest.approx(amt)
    panel2, _, _ = make_panel(seed=13)
    for name in ("close", "avg_price", "value", "trust_net", "adj_close"):
        getattr(panel2, name).loc[dates[91]:] = 7.0
    f2 = fa.compute_factors(panel2)
    for k in ("inst10", "mom20s5"):
        pd.testing.assert_series_equal(f[k].loc[d], f2[k].loc[d])


# ------------------------------------------------------------------ 核心＋衛星
def _setup(seed=14, sat_n=6):
    panel, dates, tickers = make_panel(seed=seed, n_tickers=60, n_days=130)
    f = fa.compute_factors(panel)
    caps, _ = mc.market_cap(panel, shares=pd.Series(np.arange(1, 61) * 1e6, index=panel.close.columns))
    strat = CoreSatellite("t", CoreSatelliteParams(core_k=28 - sat_n, sat_n=sat_n, min_adv=0.0))
    return panel, dates, f, caps, strat


def test_decide_structure():
    panel, dates, f, caps, strat = _setup()
    D = dates[80]
    r = strat.decide(fa.factors_on(f, D), caps.loc[D], {}, None)
    w = r.weights
    core, sat = r.selected[:22], r.selected[22:]
    assert len(w) == 28 and len(core) == 22 and len(sat) == 6 and not set(core) & set(sat)
    assert set(core) == set(caps.loc[D].sort_values(ascending=False).index[:22])      # 核心 = 市值前 22
    p = strat.params
    assert sum(w[t] for t in core) == pytest.approx(0.65 * 0.97) and sum(w[t] for t in sat) == pytest.approx(0.35 * 0.97)
    assert r.cash == pytest.approx(0.03)
    core_w = {t: w[t] / (0.65 * 0.97) for t in core}
    cap_core = caps.loc[D, core].sort_values(ascending=False)
    ref = mc.capped_cap_weights(cap_core)
    assert all(core_w[t] == pytest.approx(ref[t]) for t in core)                       # 核心 = 市值加權＋上限
    assert max(w.values()) <= 0.245 and max(w[t] for t in w if t != "2330") <= 0.095


def test_decide_is_deterministic_and_uses_only_given_inputs():
    panel, dates, f, caps, strat = _setup(seed=15)
    D = dates[80]
    a = strat.decide(fa.factors_on(f, D), caps.loc[D], {}, None).weights
    b = strat.decide(fa.factors_on(f, D), caps.loc[D], {}, None).weights
    assert a == b


def test_satellite_hysteresis_keeps_incumbents():
    panel, dates, f, caps, strat = _setup(seed=16)
    D1, D2 = dates[80], dates[85]
    r1 = strat.decide(fa.factors_on(f, D1), caps.loc[D1], {}, None)
    sat1 = set(r1.selected[22:])
    r2 = strat.decide(fa.factors_on(f, D2), caps.loc[D2], r1.weights, None)
    sat2 = set(r2.selected[22:])
    assert len(sat1 & sat2) >= 3                                # 遲滯 + 無交易帶使衛星不會全換


def test_core_satellite_backtest_rebalances_every_5_days(monkeypatch):
    panel, dates, f, caps, strat = _setup(seed=17)
    calls = []
    orig = strat.decide
    monkeypatch.setattr(strat.__class__, "decide", lambda self, *a: (calls.append(1), orig(*a))[1])
    r = bt.run_backtest(panel, strat, f, None, caps=caps)
    n = len(r.daily)
    assert len(calls) == -(-n // 5)                             # 第 1、6、11… 天才決策
    off = r.daily[(np.arange(n) % 5) != 0]
    assert (off["n_orders"] == 0).all() and (off["fee"] == 0).all()
    assert r.daily["n_hold"].iloc[-1] == 28 and r.violations == {"holdings": 0, "weight": 0, "cash": 0}


def test_cap_weight_benchmark_and_summary_costs():
    panel, dates, f, caps, strat = _setup(seed=18)
    b = bt.cap_weight_benchmark(panel, caps)
    assert b.index.is_unique and b.iloc[0] == 1.0
    # 手算一天：前一日市值定權重、當日還原報酬加權
    i = 90
    w = mc.capped_cap_weights(caps.loc[dates[i - 1]])
    px = panel.adj_close
    exp = sum(w[t] * (px.at[dates[i], t] / px.at[dates[i - 1], t] - 1) for t in w) / sum(w.values())
    assert b[dates[i]] / b[dates[i - 1]] - 1 == pytest.approx(exp)
    r = bt.run_backtest(panel, strat, f, None, caps=caps)
    s = r.summary()
    d = r.daily
    assert s["cost_total"] == pytest.approx(d["fee"].sum() + d["tax"].sum())
    pnl = r.nav.iloc[-1] - r.nav.iloc[0]
    assert s["cost_drag_ann"] > 0
    assert (s["cost_share_of_gross"] == pytest.approx(s["cost_total"] / (pnl + s["cost_total"]))) if pnl > 0 \
        else np.isnan(s["cost_share_of_gross"])
    sub = r.summary(dates[100], None)
    assert sub["days"] == len(d[d.index >= dates[100]])
