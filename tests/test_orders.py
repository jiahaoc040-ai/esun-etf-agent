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


# ------------------------------------------------------------------ 合規保險（plan）
def test_stress_cap_buys_limits_buy_add_targets():
    from esun_agent.agent.plan import stress_cap_buys
    out, notes = stress_cap_buys({"2317": 0.095, "2330": 0.24, "2454": 0.095, "2412": 0.05},
                                 {"2454": 0.098, "2412": 0.01})
    assert out["2317"] < 0.10 / 1.10 and out["2330"] < 0.25 / 1.10 and out["2412"] == 0.05
    assert out["2454"] == 0.095                                     # 目標 < 現權重（減碼）不受影響
    assert len(notes) == 2


def test_plan_orders_buy_vs_add_and_stress():
    from esun_agent.agent.plan import plan_orders
    close = {"2317": 100.0, "2330": 1000.0}
    p = plan_orders({"2317": 0.12, "2330": 0.30}, 1e8, 9e7, close, {"2330": 1000})
    acts = {d["ticker"]: d["action"] for d in p.decisions}
    assert acts == {"2317": "BUY", "2330": "ADD"}
    shares = {o.ticker: o.shares for o in p.orders}
    assert shares["2317"] * 100 * 1.1 / 1e8 <= 0.10 and (1000 + shares["2330"]) * 1000 * 1.1 / 1e8 <= 0.25


def test_force_passive_trims_and_protect_from_deferral():
    from esun_agent.agent.plan import force_passive_trims, limit_decisions, passive_overweight
    cur = {"2317": 0.097, "2330": 0.245, "2412": 0.05}
    assert set(passive_overweight(cur)) == {"2317", "2330"}
    cap = lambda t: 0.20 if t == "2330" else 0.08
    tgt, done = force_passive_trims({"2317": 0.097, "2330": 0.245, "2412": 0.05}, {"2317": 0.08, "2330": 0.15},
                                    cur, {"2317", "2330"}, cap)
    assert done == {"2317": 0.08, "2330": 0.15} and tgt["2412"] == 0.05
    # 策略目標仍 ≥ 現權重（例如凍結）→ 至少降到內部上限
    tgt, done = force_passive_trims({"2317": 0.097}, {"2317": 0.12}, {"2317": 0.097}, {"2317"}, cap)
    assert done["2317"] == 0.08
    # 異動檔數超限時，被保護的強制減碼不會被延後
    close = {f"{1101 + i}": 10.0 for i in range(5)}
    hold = {t: 10_000 for t in close}                                # 每檔 0.1%（nav 1e8）
    target = {t: 0.0005 for t in close}                               # 全部小幅 TRIM
    t2, notes = limit_decisions(target, 1e8, close, hold, max_n=2, protect={"1101", "1102"})
    assert t2["1101"] == 0.0005 and t2["1102"] == 0.0005 and any(n.startswith("deferred:") for n in notes)
