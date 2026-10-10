"""用真實資料（data/market 等）跑隨機 tilt 模擬：框架不得違規，Active Share 不得連續 2 日 < 20%。"""
import pytest

from esun_agent import backtest as bt
from esun_agent.data.etf_holdings import load_top10
from esun_agent.data.prices import load_panel
from esun_agent.strategy.factors import compute_factors
from esun_agent.strategy.final import FinalStrategy, random_tilt_fn
from esun_agent.strategy.marketcap import market_cap


@pytest.fixture(scope="module")
def env():
    panel = load_panel()
    return panel, compute_factors(panel), load_top10(), market_cap(panel)[0]


def consecutive(mask) -> int:
    best = run_len = 0
    for x in mask:
        run_len = run_len + 1 if x else 0
        best = max(best, run_len)
    return best


@pytest.mark.parametrize("seed,period", [(0, 5), (1, 5), (2, 1), (13, 5)])
def test_real_data_random_tilts_no_violations_no_two_consecutive_ap_breaches(env, seed, period):
    panel, factors, top10, caps = env
    r = bt.run_backtest(panel, FinalStrategy(), factors, top10, caps=caps, tilt_fn=random_tilt_fn(seed, period=period))
    d = r.daily
    assert r.violations == {"holdings": 0, "weight": 0, "cash": 0}
    assert d["n_hold"].between(20, 30).all()
    assert consecutive((d["ap_min"] < 0.20).to_numpy()) <= 1


def test_real_data_no_tilt_never_breaches(env):
    panel, factors, top10, caps = env
    r = bt.run_backtest(panel, FinalStrategy(), factors, top10, caps=caps)
    assert r.violations == {"holdings": 0, "weight": 0, "cash": 0}
    assert consecutive((r.daily["ap_min"] < 0.20).to_numpy()) <= 1
