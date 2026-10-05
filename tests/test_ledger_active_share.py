import pytest

from esun_agent.active_share import active_share, check_active_share
from esun_agent.ledger import Ledger
from esun_agent.universe import authority_for, load_active_etfs, load_universe


def test_reference_lists():
    u = load_universe()
    assert len(u) == 150
    assert sum(v["market"] == "TWSE" for v in u.values()) == 100
    assert authority_for("2330") == "twse" and authority_for("5274") == "tpex"
    assert len(load_active_etfs()) == 30


def test_ledger_fees_and_tax():
    L = Ledger(cash=1_000_000)
    L.apply_fills([{"ticker": "2330", "side": "BUY", "shares": 1000}], {"2330": 500})
    assert L.cash == pytest.approx(1_000_000 - 500_000 * 1.001425)
    L.apply_fills([{"ticker": "2330", "side": "SELL", "shares": 1000}], {"2330": 600})
    assert L.cash == pytest.approx(1_000_000 - 500_000 * 1.001425 + 600_000 * (1 - 0.001425 - 0.003))
    assert L.holdings == {}


def test_ledger_oversell():
    with pytest.raises(ValueError):
        Ledger(cash=0, holdings={"2330": 1000}).apply_fills(
            [{"ticker": "2330", "side": "SELL", "shares": 2000}], {"2330": 1})


def test_reconcile():
    L = Ledger(cash=100, holdings={"2330": 1000})
    assert L.reconcile({"2330": 2000}) == ["2330: 自記 1000 ≠ 官方 2000"]


def test_active_share_identical_is_zero():
    w = {str(1000 + i): 0.1 for i in range(10)}
    assert active_share(w, w) == pytest.approx(0)
    assert active_share(w, {"9999": 1.0}) == pytest.approx(1.0)


def test_check_active_share_flags_copycat():
    etf = {str(1000 + i): 0.08 for i in range(10)}
    mine = {**{str(1000 + i): 0.07 for i in range(10)}, "2330": 0.05}
    r = check_active_share(mine, {"00981A": etf})
    assert not r["ok"] and r["worst_etf"] == "00981A"
