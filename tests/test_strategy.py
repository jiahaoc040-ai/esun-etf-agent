import numpy as np
import pandas as pd
import pytest

from esun_agent.strategy import factors as fa
from esun_agent.strategy import portfolio as po
from .helpers_market import make_panel

P = po.PortfolioParams()


# ------------------------------------------------------------------ 因子
def test_factor_values_and_no_lookahead():
    panel, dates, tickers = make_panel(seed=1)
    f = fa.compute_factors(panel)
    d = dates[80]
    t = tickers[3]
    px = panel.adj_close[t]
    assert f["mom20"].loc[d, t] == pytest.approx(px.loc[d] / px.loc[dates[60]] - 1)
    ret = px.pct_change()
    assert f["vol20"].loc[d, t] == pytest.approx(ret.loc[dates[61]:d].std())
    fn = panel.foreign_net[t].loc[dates[61]:d].sum() / panel.volume[t].loc[dates[61]:d].sum()
    assert f["foreign20"].loc[d, t] == pytest.approx(fn)
    v = panel.value[t]
    assert f["valchg"].loc[d, t] == pytest.approx(np.log(v.loc[dates[76]:d].mean() / v.loc[dates[61]:d].mean()))
    # 把 d 之後的資料全部改掉，d 當日（含以前）的因子不變
    panel2, _, _ = make_panel(seed=1)
    for name in ("close", "volume", "value", "foreign_net", "trust_net", "adj_close"):
        getattr(panel2, name).loc[dates[81]:] = 12345.0
    f2 = fa.compute_factors(panel2)
    for k in f:
        pd.testing.assert_series_equal(f[k].loc[d], f2[k].loc[d])


def test_split_adjusted_momentum_has_no_jump():
    panel, dates, _ = make_panel(seed=2, splits={("1005", 70): 4})
    f = fa.compute_factors(panel)
    assert abs(f["mom5"].loc[dates[72], "1005"]) < 0.25       # 未還原會是 −75% 左右
    raw = panel.close["1005"]
    assert raw.loc[dates[72]] / raw.loc[dates[65]] - 1 < -0.6


def test_eligibility_excludes_new_and_halted():
    panel, dates, tickers = make_panel(seed=3, halt={"1002": (70, 90)})
    f = fa.compute_factors(panel)
    elig = fa.eligible(fa.factors_on(f, dates[75]))
    assert "1002" not in elig and "1003" in elig
    assert len(fa.eligible(fa.factors_on(f, dates[30]))) == 0         # 歷史不足 61 天
    big = fa.eligible(fa.factors_on(f, dates[100]), min_adv=1e18)
    assert len(big) == 0                                              # 流動性門檻


def test_score_zscore_and_sign():
    idx = pd.Index(list("abcde"))
    fdf = pd.DataFrame({n: np.arange(5.0) for n in fa.FACTOR_NAMES}, index=idx)
    s = fa.score(fdf, {"mom20": 1.0, "vol20": -1.0}, idx)
    assert s.abs().max() < 1e-12                                        # 正負抵銷
    s2 = fa.score(fdf, {"mom20": 1.0}, idx)
    assert list(s2.index) == list("edcba") and s2.mean() == pytest.approx(0)


# ------------------------------------------------------------------ 投組
def scores(n=40, seed=0):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(size=n), index=[str(1001 + i) for i in range(n)]).sort_values(ascending=False)


def test_select_holdings_range_and_hysteresis():
    sc = scores()
    sel = po.select_holdings(sc, set(), P)
    assert len(sel) == 25 and set(sel) == set(sc.index[:25])
    # 排名 25~27 的現有持股保留；排名 28 以後被換掉
    cur = {sc.index[26], sc.index[30]}
    sel2 = po.select_holdings(sc, cur, P)
    assert sc.index[26] in sel2 and sc.index[30] not in sel2 and 22 <= len(sel2) <= 28
    many = set(sc.index[:28])                                           # 持股很多時不超過 max_hold
    assert len(po.select_holdings(sc, many | set(sc.index[28:30]), P)) <= 28


def test_raw_weights_caps_and_cash():
    sc = scores()
    w = po.raw_weights(po.select_holdings(sc, set(), P), sc, P)
    assert sum(w.values()) == pytest.approx(1 - P.cash_target)
    assert max(w.values()) <= P.soft_cap + 1e-12 and 0.01 <= 1 - sum(w.values()) <= 0.22


def test_tsmc_handled_separately():
    sc = scores()
    sc.loc["2330"] = 5.0
    sc = sc.sort_values(ascending=False)
    w = po.raw_weights(po.select_holdings(sc, set(), P), sc, P)
    assert w["2330"] == P.tsmc_weight
    big = po.PortfolioParams(tsmc_weight=0.40)
    assert po.raw_weights(po.select_holdings(sc, set(), big), sc, big)["2330"] == pytest.approx(0.245)  # 25% − 0.5%
    others = [v for t, v in w.items() if t != "2330"]
    assert max(others) <= P.soft_cap + 1e-12 and sum(w.values()) == pytest.approx(0.97)


def test_waterfill_respects_caps():
    w = po._waterfill({"a": 10, "b": 1, "c": 1}, 0.6, {"a": 0.3, "b": 0.5, "c": 0.5})
    assert w["a"] == pytest.approx(0.3) and w["b"] == pytest.approx(0.15) and sum(w.values()) == pytest.approx(0.6)


def test_no_trade_band():
    p = po.PortfolioParams(cash_low=0.0, cash_high=1.0)
    tgt = {"a": 0.06, "b": 0.06, "c": 0.04, "d": 0.03, "e": 0.0}
    cur = {"a": 0.055, "b": 0.09, "c": 0.0, "d": 0.0, "e": 0.01}
    out = po.apply_no_trade_band(tgt, cur, p)
    assert out["a"] == pytest.approx(0.055)                 # 差 0.5% < 1.5% → 不動
    assert out["b"] != pytest.approx(0.09)                  # 差 3% → 交易
    assert out["c"] > 0 and out["d"] > 0 and out["e"] == 0  # 新進場 / 完全出場不受無交易帶限制
    assert sum(out.values()) == pytest.approx(sum(tgt.values()))   # 缺口由未鎖定者吸收，現金不變


def test_band_does_not_lock_overweight_names():
    p = po.PortfolioParams(soft_cap=0.05, cash_low=0.0, cash_high=1.0)
    out = po.apply_no_trade_band({"a": 0.05, "b": 0.05}, {"a": 0.051, "b": 0.05}, p)
    assert out["a"] == 0.05 and out["b"] == 0.05            # a 現權重 5.1% 已超過上限 5% → 回到目標


def test_band_gives_up_when_nothing_free_to_absorb():
    p = po.PortfolioParams(cash_low=0.0, cash_high=1.0)
    tgt = {"a": 0.06, "b": 0.05}
    out = po.apply_no_trade_band(tgt, {"a": 0.055, "b": 0.05}, p)   # 全部被鎖、缺口無人吸收
    assert out == tgt
    p2 = po.PortfolioParams(cash_low=0.01, cash_high=0.22)
    tgt2 = {"a": 0.06, "b": 0.06}                                    # 現金 88%，鎖定後仍在範圍外 → 放棄
    assert po.apply_no_trade_band(tgt2, {"a": 0.055, "b": 0.0}, p2) == tgt2


def test_active_share_fit_reduces_overlap():
    sc = scores()
    sel = po.select_holdings(sc, set(), P)
    w0 = po.raw_weights(sel, sc, P)
    top = sorted(w0, key=lambda t: -w0[t])[:10]
    etf = {"00001A": {t: 0.1 for t in top}}                  # 與我方前 10 大完全相同的 ETF
    from esun_agent.data.etf_holdings import check_target_weights
    assert not check_target_weights(w0, etf, expected_etfs=["00001A"])["ok"]
    w1, res = po.fit_active_share(w0, sc, etf, P)
    assert res["ok"] and sum(w1.values()) == pytest.approx(sum(w0.values()))
    assert max(w1.values()) <= P.soft_cap + 1e-9


def test_construct_portfolio_end_to_end():
    sc = scores()
    r = po.construct_portfolio(sc, {}, P, None)
    assert 22 <= len(r.weights) <= 28 and r.cash == pytest.approx(P.cash_target)
    with pytest.raises(ValueError):
        po.construct_portfolio(sc.iloc[:10], {}, P, None)
