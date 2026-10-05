"""官方委託公式：以 D-Plan 正例 (TEAM_042, 2026-10-27) 的數字為準。"""
import json
from pathlib import Path

from esun_agent.orders import derive_orders, target_shares

ROOT = Path(__file__).resolve().parent.parent
CTX = json.loads((ROOT / "tests/fixtures/ctx_example_2026-10-27.json").read_text(encoding="utf-8"))


def test_target_shares_official_examples():
    assert target_shares(0.23, 102_000_000, 1480) == 15_000
    assert target_shares(0.045, 102_000_000, 42.5) == 108_000
    assert target_shares(0.03, 102_000_000, 1250) == 2_000


def test_floor_not_round():
    # 0.0999 張 → 0；絕不四捨五入成 1 張
    assert target_shares(0.01, 1_000_000, 100.1) == 0


def test_float_noise_does_not_drop_a_lot():
    # 0.1 * 1e9 / 100 / 1000 = 1000 張；浮點誤差不得變成 999
    assert target_shares(0.1, 1_000_000_000, 100) == 1_000_000


def test_positive_example_orders_match_exactly():
    doc = json.loads((ROOT / "docs/competition/D-Plan_TEAM_042_2026-10-27.json").read_text(encoding="utf-8"))
    got = [o.to_dict() for o in derive_orders(doc["decisions"], CTX["prev_nav"], CTX["prev_close"], CTX["holdings"])]
    key = lambda o: (o["ticker"], o["side"], o["shares"], o["decision_ref"])
    assert [key(o) for o in got] == [key(o) for o in doc["orders"]]
    assert [key(o) for o in got] == [("2330", "SELL", 2000, "D1"), ("2891", "BUY", 108000, "D2")]


def test_no_order_when_already_at_target():
    d = [{"decision_id": "D1", "ticker": "2330", "target_weight": 0.23}]
    assert derive_orders(d, 102_000_000, {"2330": 1480}, {"2330": 15000}) == []


def test_sell_all():
    d = [{"decision_id": "D1", "ticker": "2412", "target_weight": 0.0}]
    (o,) = derive_orders(d, 1e8, {"2412": 120}, {"2412": 50000})
    assert (o.side, o.shares) == ("SELL", 50000)
