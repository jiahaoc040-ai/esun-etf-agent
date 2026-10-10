import copy
import json

import numpy as np
import pandas as pd
import pytest

from esun_agent import backtest as bt
from esun_agent.data.etf_holdings import check_target_weights
from esun_agent.strategy import factors as fa
from esun_agent.strategy import final as fi
from esun_agent.strategy import marketcap as mc
from esun_agent.strategy.declaration import DECLARATION_PATH, load_declaration, validate_declaration

from .helpers_market import make_panel

P = fi.FinalParams(min_adv=0.0)
TICKERS = ["2330"] + [str(1001 + i) for i in range(59)]


@pytest.fixture(autouse=True)
def synthetic_universe(monkeypatch):
    monkeypatch.setattr(fi, "load_universe", lambda: {t: {"name": t, "market": "TWSE"} for t in TICKERS})


def setup(seed=31, n_days=130):
    panel, dates, tickers = make_panel(seed=seed, n_tickers=60, n_days=n_days)
    f = fa.compute_factors(panel)
    caps, _ = mc.market_cap(panel, shares=pd.Series(np.arange(1, 61) * 1e6, index=panel.close.columns))
    return panel, dates, f, caps


def fake_etf(w, n=10, mult=1.0):
    """ETF 前 10 大 = 我方前 10 大（乘上 mult）。"""
    top = sorted(w, key=lambda t: -w[t])[:n]
    return {"E": {t: w[t] * mult for t in top}}


def flat_base(n=28, cash=0.03):
    names = TICKERS[1:n + 1]
    return {t: (1 - cash) / n for t in names}


# ------------------------------------------------------------------ base_weights
def test_base_weights_structure_and_no_lookahead():
    panel, dates, f, caps = setup()
    st = fi.FinalStrategy(params=P).bind(f, caps, None)
    D = dates[90]
    w = st.base_weights(D)
    assert len(w) == 28 and sum(w.values()) == pytest.approx(0.97)
    assert max(w.values()) <= 0.20 + 1e-9 and max(v for t, v in w.items() if t != "2330") <= 0.08 + 1e-9
    b = mc.capped_cap_weights(caps.loc[D].dropna())
    assert set(w) == set(sorted(b, key=lambda t: (-b[t], t))[:28])
    panel2, _, _ = make_panel(seed=31, n_tickers=60, n_days=130)
    for name in ("close", "volume", "value", "adj_close", "avg_price", "foreign_net", "trust_net"):
        getattr(panel2, name).loc[dates[91]:] *= 5
    f2 = fa.compute_factors(panel2)
    caps2, _ = mc.market_cap(panel2, shares=pd.Series(np.arange(1, 61) * 1e6, index=panel2.close.columns))
    assert fi.FinalStrategy(params=P).bind(f2, caps2, None).base_weights(D) == w
    with pytest.raises(RuntimeError):
        fi.FinalStrategy(params=P).base_weights(D)


def test_base_weights_active_share_target_25():
    panel, dates, f, caps = setup(seed=32)
    D = dates[90]
    raw = fi.FinalStrategy(params=P).bind(f, caps, None).base_weights(D)
    etf = fake_etf(raw, mult=1.0)
    assert check_target_weights(raw, etf, margin=0.0, expected_etfs=["E"])["min"] < 0.27
    w, _, notes = fi.base_plan(fa.factors_on(f, D), caps.loc[D], P, etf)
    ap = check_target_weights(w, etf, margin=0.0, expected_etfs=["E"])
    assert ap["min"] >= 0.27 - 1e-9 and sum(w.values()) == pytest.approx(0.97)
    assert notes and notes[0].startswith("base_active_share_fix:")
    assert fi.FinalStrategy(params=P).bind(f, caps, etf).base_weights(D) == w


def test_ensure_active_share_minimal_then_escalates():
    w = flat_base()
    names = sorted(w, key=lambda t: (-w[t], t))
    pp = P.portfolio_params()
    partial = {"E": {**{t: 0.1 for t in names[:8]}, TICKERS[50]: 0.1, TICKERS[51]: 0.1}}   # 與我方前 10 大重疊 8 檔
    assert check_target_weights(w, partial, margin=0.0, expected_etfs=["E"])["min"] < 0.27
    out, res, tier = fi.ensure_active_share(w, partial, pp)
    assert tier == "minimal" and res["min"] >= 0.27 - 1e-9
    assert len([t for t in w if out[t] < w[t] - 1e-9]) <= 3                      # 最小調整只減 1–3 檔
    many = {f"E{i}": {t: w[t] for t in names[i * 3:i * 3 + 10]} for i in range(5)}  # 5 檔 ETF 的前 10 大輪流蓋住全部持股
    out, res, tier = fi.ensure_active_share(w, many, pp)
    assert tier == "full" and res["min"] >= 0.27 - 1e-9
    assert fi.ensure_active_share(w, None, pp) == (w, None, "none")
    far = {"E": {t: 0.1 for t in TICKERS[40:50]}}
    assert fi.ensure_active_share(w, far, pp)[2] == "none"


# ------------------------------------------------------------------ apply_tilts：驗證
def test_validate_and_strict_errors():
    base = flat_base()
    held, extra = TICKERS[1], TICKERS[40]
    assert fi.validate_tilts(base, {held: 0.02, extra: 0.01}, P) == []
    cases = {
        "超過單檔上限": {held: 0.031},
        "不在 150 檔名單": {"9999": 0.01},
        "不是有限數字": {held: float("nan")},
        "不可放空": {extra: -0.01},
        "流動性不足": {extra: 0.01},
        "主動偏離": {t: 0.03 * (1 if i % 2 else -1) for i, t in enumerate(TICKERS[1:13])},
    }
    for key, tilts in cases.items():
        errs = fi.validate_tilts(base, tilts, P, eligible=set(TICKERS[1:30]) if key == "流動性不足" else None)
        assert any(key in e for e in errs), (key, errs)
    with pytest.raises(fi.TiltError) as ei:
        fi.apply_tilts(base, {held: 0.05, "9999": 0.01}, params=P)
    assert len(ei.value.errors) == 2


def test_non_strict_drops_bad_and_scales_total():
    base = flat_base()
    tilts = {TICKERS[1]: 0.05, TICKERS[2]: 0.02, "9999": 0.01}
    r = fi.apply_tilts(base, tilts, params=P, strict=False)
    assert r.applied.get(TICKERS[2]) == pytest.approx(0.02) and TICKERS[1] not in r.applied and "9999" not in r.weights
    assert sum(n.startswith("tilt_dropped") for n in r.notes) == 2
    many = {t: 0.03 for t in TICKERS[1:13]}                              # Σ|d|/2 = 18% > 15%
    r2 = fi.apply_tilts(base, many, params=P, strict=False)
    assert r2.active_share_total <= 0.15 + 1e-9 and any(n.startswith("tilt_scaled") for n in r2.notes)


# ------------------------------------------------------------------ apply_tilts：套用與重新檢查
def test_apply_basic_add_and_trim_keeps_invariants():
    base = flat_base()
    new_name = TICKERS[40]
    r = fi.apply_tilts(base, {TICKERS[1]: 0.02, TICKERS[2]: -0.03, new_name: 0.02}, params=P)
    w = r.weights
    assert w[TICKERS[1]] == pytest.approx(base[TICKERS[1]] + 0.02)
    assert w[TICKERS[2]] == pytest.approx(base[TICKERS[2]] - 0.03) and w[new_name] == pytest.approx(0.02)
    assert r.n_hold == 29 and r.cash == pytest.approx(0.03 + 0.03 - 0.02 - 0.02)   # 現金 = 3% − 淨加碼
    assert r.requested == {TICKERS[1]: 0.02, TICKERS[2]: -0.03, new_name: 0.02}
    assert r.active_share_total == pytest.approx(0.035)


def test_cap_clip_for_tsmc_and_others():
    base = {"2330": 0.20, **{t: 0.74 / 27 for t in TICKERS[1:28]}}               # 合計 94%，現金 6%
    r = fi.apply_tilts(base, {"2330": 0.03}, params=P)
    assert r.weights["2330"] == pytest.approx(0.22) and "cap_clipped:2330" in r.notes
    base2 = {TICKERS[1]: 0.08, **{t: 0.86 / 27 for t in TICKERS[2:29]}}            # 合計 94%
    r2 = fi.apply_tilts(base2, {TICKERS[1]: 0.03}, params=P)
    assert r2.weights[TICKERS[1]] == pytest.approx(0.085) and "cap_clipped:" + TICKERS[1] in r2.notes


def test_max_hold_drops_smallest_added():
    base = flat_base()
    adds = {TICKERS[40 + i]: 0.005 * (i + 1) for i in range(4)}        # +4 檔 → 32 檔
    r = fi.apply_tilts(base, adds, params=P)
    assert r.n_hold == 29 and all(f"max_hold_dropped:{TICKERS[40 + i]}" in r.notes for i in range(3))   # 32 → 29，丟最小的 3 檔
    assert TICKERS[43] in r.weights and TICKERS[40] not in r.weights


def test_min_hold_error():
    base = flat_base()
    strict_hold = fi.FinalParams(min_adv=0.0, min_hold=29)
    with pytest.raises(fi.TiltError, match="不在 29–29 檔"):
        fi.apply_tilts(base, {TICKERS[1]: 0.01}, params=strict_hold)


def test_cash_floor_scales_buys():
    base = flat_base()
    tilts = {t: 0.03 for t in TICKERS[1:6]}                              # 淨加碼 15% → 現金 −12% → 必須縮小
    r = fi.apply_tilts(base, tilts, params=P)
    assert r.cash >= P.cash_low - 1e-9 and any(n.startswith("cash_scaled") for n in r.notes)
    assert all(0 < d <= 0.03 for d in r.applied.values())


def test_active_share_rechecked_after_tilts():
    base = flat_base()
    pp = P.portfolio_params()
    etf = {"E": {t: 0.1 for t in TICKERS[30:40]}}                       # 與基準無重疊 → 基準 AP 很高
    r0 = fi.apply_tilts(base, {TICKERS[1]: 0.01}, params=P, top10=etf)
    assert "active_share_fix" not in " ".join(r0.notes)
    # 把 tilt 全部加到 ETF 前 10 大 → 重疊變大 → AP 掉到 25% 以下 → 需要修正
    tilts = {t: 0.03 for t in TICKERS[30:34]}
    etf2 = {"E": {t: base.get(t, 0.0) + 0.03 for t in TICKERS[30:40]}}
    r = fi.apply_tilts(base, tilts, params=P, top10=etf2)
    ap = check_target_weights(r.weights, etf2, margin=0.0, expected_etfs=["E"])
    assert ap["min"] >= 0.27 - 1e-6 or any("active_share" in n for n in r.notes)
    assert ap["ok"] and sum(r.weights.values()) <= 1 - P.cash_low + 1e-9
    assert pp.ap_margin == pytest.approx(0.07)


def test_hard_active_share_failure():
    base = flat_base()
    etf = {"E": {t: base[t] for t in TICKERS[1:11]}}
    tight = fi.FinalParams(min_adv=0.0, ap_max_iter=0)                   # 修正無法運作 → 低於官方門檻
    with pytest.raises(fi.TiltError, match="低於官方門檻"):
        fi.apply_tilts(base, {TICKERS[1]: 0.01}, params=tight, top10=etf)
    r = fi.apply_tilts(base, {TICKERS[1]: 0.01}, params=tight, top10=etf, strict=False)
    assert "active_share_below_official" in r.notes


# ------------------------------------------------------------------ 策略與回測整合
def test_decide_falls_back_to_base_on_bad_tilts():
    panel, dates, f, caps = setup(seed=33)
    D = dates[90]
    st = fi.FinalStrategy(params=P)
    fdf = fa.factors_on(f, D)
    plain = st.decide(fdf, caps.loc[D], {}, None)
    bad = st.decide(fdf, caps.loc[D], {}, None, {"9999": 0.03, TICKERS[1]: 0.5})
    assert bad.weights == plain.weights and any(n.startswith("tilt_dropped") for n in bad.notes)


def test_force_rebalance_triggers():
    st = fi.FinalStrategy(params=P)
    assert st.force_rebalance({"2330": 0.241}, None) and st.force_rebalance({"1001": 0.096}, None)
    assert not st.force_rebalance({"2330": 0.20, "1001": 0.05}, None)
    w = flat_base()
    etf = {"E": {t: w[t] for t in TICKERS[1:11]}}                       # AP ≈ 0 → 觸發
    assert st.force_rebalance(w, etf)


def run(panel, f, caps, tilt_fn=None, top10=None):
    return bt.run_backtest(panel, fi.FinalStrategy(params=P), f, top10, caps=caps, tilt_fn=tilt_fn)


def test_tilt_change_triggers_same_day_trade_only_when_changed():
    panel, dates, f, caps = setup(seed=34)
    r0 = run(panel, f, caps)
    sched = {d for i, d in enumerate(r0.daily.index) if i % 5 == 0}
    assert set(r0.daily.index[r0.daily["rebalanced"]]) >= sched          # 排程日一定交易
    extra0 = set(r0.daily.index[r0.daily["rebalanced"]]) - sched

    const = lambda D, elig, cur: {TICKERS[40]: 0.02}                     # noqa: E731  tilt 固定不變
    rc = run(panel, f, caps, const)
    assert set(rc.daily.index[rc.daily["rebalanced"]]) - sched == extra0  # 沒有變動 → 不會多交易

    switch_D = dates[102]                                                # 決策日 D：tilt 從這天起改變
    switch_T = dates[103]                                                # 對應的交易日 T
    assert switch_T not in sched                                         # 這天原本不是排程日
    def fn(D, elig, cur):
        return {TICKERS[40]: 0.02} if D < switch_D else {TICKERS[41]: 0.02}
    rs = run(panel, f, caps, fn)
    assert bool(rs.daily.loc[switch_T, "rebalanced"])                    # tilt 變動 → 當天就決策、交易
    assert rs.daily.loc[switch_T, "n_orders"] > 0
    ex = set(rs.daily.index[rs.daily["rebalanced"]]) - sched - extra0
    assert ex <= {switch_T}                                              # 除了漂移保護外，只在變動當天多交易


def test_random_tilt_fn_properties():
    fn = fi.random_tilt_fn(7, period=5)
    elig = TICKERS[:40]
    cur = {t: 0.03 for t in TICKERS[1:20]}
    days = [f"2026-01-{d:02d}" for d in range(1, 13)]
    outs = [fn(D, elig, cur) for D in days]
    assert outs[0] == outs[4] and outs[5] == outs[9] and outs[0] != outs[5]      # 5 日一組
    assert len(outs[0]) == 5 and all(0.01 <= abs(d) <= 0.03 for d in outs[0].values())
    assert all(d > 0 or t in cur for out in outs for t, d in out.items())          # 減碼只選現有持股
    again = fi.random_tilt_fn(7, period=5)
    assert [again(D, elig, cur) for D in days] == outs                              # 同 seed 可重現
    daily = fi.random_tilt_fn(7, period=1)
    assert daily(days[0], elig, cur) != daily(days[1], elig, cur)


def test_random_tilt_simulations_never_violate_rules():
    panel, dates, f, caps = setup(seed=35)
    etf = fake_etf(fi.FinalStrategy(params=P).bind(f, caps, None).base_weights(dates[90]))
    for seed in range(4):
        for period in (5, 1):
            r = run(panel, f, caps, fi.random_tilt_fn(seed, period=period), top10=etf)
            d = r.daily
            assert r.violations == {"holdings": 0, "weight": 0, "cash": 0}, (seed, period)
            assert d["n_hold"].between(20, 29).all() and d["max_w"].max() < 0.25
            assert d["cash_ratio"].between(0, 0.25).all()


# ------------------------------------------------------------------ 宣告檔
def test_declaration_loads_and_matches_code():
    d = load_declaration()
    assert d["tilt"]["limits"]["per_name_abs_delta"] == 0.03 and d["portfolio_rules"]["ap_target"] == 0.27
    ids = {b["id"] for b in d["tilt"]["allowed_bases"]}
    assert ids == {"financial_report", "investor_conference", "monthly_revenue", "trust_flow", "material_news"}
    assert d["status"] == "final" and d["etf"]["name"] == "台灣核心優選 AI 主動 ETF" and d["etf"]["name_status"] == "final"
    assert d["portfolio_rules"]["ap_target"] == 0.27 and d["portfolio_rules"]["ap_trigger"] == 0.22
    by_id = {b["id"]: b for b in d["tilt"]["allowed_bases"]}
    assert {i: set(b["authority"]) for i, b in by_id.items()} == {
        "financial_report": {"mops"}, "investor_conference": {"mops", "media"}, "monthly_revenue": {"mops"},
        "trust_flow": {"twse", "tpex"}, "material_news": {"mops"}}
    assert all(b["authority_status"] == "confirmed" for b in by_id.values())


def test_declaration_detects_drift_from_code():
    d = json.loads(DECLARATION_PATH.read_text(encoding="utf-8"))
    bad = copy.deepcopy(d)
    bad["portfolio_rules"]["max_active"] = 0.2
    assert any("max_active" in e for e in validate_declaration(bad))
    bad = copy.deepcopy(d)
    bad["tilt"]["allowed_bases"] = bad["tilt"]["allowed_bases"][:3]
    assert any("allowed_bases" in e for e in validate_declaration(bad))
    bad = copy.deepcopy(d)
    del bad["benchmark"]
    assert validate_declaration(bad) == ["缺少欄位 benchmark"]


def test_declaration_final_status_requirements():
    d = json.loads(DECLARATION_PATH.read_text(encoding="utf-8"))
    bad = copy.deepcopy(d)
    bad["etf"]["name_status"] = "draft"
    assert any("name_status" in e for e in validate_declaration(bad))
    bad = copy.deepcopy(d)
    bad["tilt"]["allowed_bases"][0]["authority_status"] = "pending"
    assert any("authority_status" in e for e in validate_declaration(bad))
    bad = copy.deepcopy(d)
    bad["status"] = "wip"
    assert any("status" in e for e in validate_declaration(bad))


# ------------------------------------------------------------------ Active Share 硬規則（< 22% → 當日必須再平衡到 ≥ 27%）
def max_consecutive(mask) -> int:
    best = run_len = 0
    for x in mask:
        run_len = run_len + 1 if x else 0
        best = max(best, run_len)
    return best


def shocked_panel(seed, targets, factors_by_day: dict, n_days=130):
    """從 dates[idx] 起把 targets 的價格乘上 factor（可疊加多天）；回傳 (panel, f, caps)。"""
    panel, dates, _ = make_panel(seed=seed, n_tickers=60, n_days=n_days)
    for idx, k in factors_by_day.items():
        for name in ("close", "open", "avg_price", "adj_close"):
            getattr(panel, name).loc[dates[idx]:, targets] *= k
        panel.value.loc[dates[idx]:, targets] *= k
    f = fa.compute_factors(panel)
    caps, _ = mc.market_cap(panel, shares=pd.Series(np.arange(1, 61) * 1e6, index=panel.close.columns))
    return panel, f, caps


def etf_for(seed):
    panel, dates, f, caps = setup(seed=seed)
    raw = fi.FinalStrategy(params=P).bind(f, caps, None).base_weights(dates[90])
    etf = fake_etf(raw)
    return etf, list(etf["E"])[:5], dates


def test_ap_trigger_and_hard_rule_in_decide():
    panel, dates, f, caps = setup(seed=36)
    D = dates[90]
    st = fi.FinalStrategy(params=P)
    fdf = fa.factors_on(f, D)
    raw = st.bind(f, caps, None).base_weights(D)
    etf = fake_etf(raw)
    ap_raw = check_target_weights(raw, etf, margin=0.0, expected_etfs=["E"])["min"]
    assert 0.20 <= ap_raw < 0.22 or ap_raw < 0.20                         # 原始基準的 Active Share 很低 → 會觸發
    assert st.force_rebalance(raw, etf)                                  # < 22% → 硬規則觸發
    fixed = st.decide(fdf, caps.loc[D], {}, etf).weights                 # 空倉決策 → AP ≥ 27%
    ap_fixed = check_target_weights(fixed, etf, margin=0.0, expected_etfs=["E"])["min"]
    assert ap_fixed >= 0.27 - 1e-9 and not st.force_rebalance(fixed, etf)
    # 現權重 AP < 22%：不受無交易帶限制（把帶寬放到 50% 也一樣）→ 目標 AP ≥ 27%
    wide = fi.FinalStrategy(params=fi.FinalParams(min_adv=0.0, band=0.5))
    r = wide.decide(fdf, caps.loc[D], raw, etf)
    assert "ap_forced_rebalance" in r.notes
    assert check_target_weights(r.weights, etf, margin=0.0, expected_etfs=["E"])["min"] >= 0.27 - 1e-9
    # AP ≥ 22% 時不觸發（即使 < 27%）
    mid = {"E": {t: w for t, w in list(raw.items())[:10]}}
    ap_mid = check_target_weights(fixed, mid, margin=0.0, expected_etfs=["E"])["min"]
    assert (ap_mid < 0.22) == st.force_rebalance(fixed, mid)


def test_recovery_target_after_a_breach_day():
    panel, dates, f, caps = setup(seed=37)
    D = dates[90]
    st = fi.FinalStrategy(params=P)
    raw = st.bind(f, caps, None).base_weights(D)
    etf = fake_etf(raw)
    assert check_target_weights(raw, etf, margin=0.0, expected_etfs=["E"])["min"] < 0.20     # 昨天已違規
    r = st.decide(fa.factors_on(f, D), caps.loc[D], raw, etf)
    assert "ap_recovery" in r.notes and "ap_forced_rebalance" in r.notes
    assert check_target_weights(r.weights, etf, margin=0.0, expected_etfs=["E"])["min"] >= 0.34 - 1e-9
    ok = st.decide(fa.factors_on(f, D), caps.loc[D], {}, etf)
    assert "ap_recovery" not in ok.notes                                                  # 沒有昨天違規 → 目標 27%


# 台股單日漲跌幅限制 ±10%：連續兩日各 +30% 的路徑不可能發生，所以不納入保證範圍；
# 下面用「單日超額衝擊」（1.15–1.5 倍，已超過漲停）與「連續漲停／跌停」兩種壓力情境。
@pytest.mark.parametrize("k", [1.15, 1.3, 1.5])
def test_single_shock_forces_next_day_rebalance_off_schedule(k):
    etf, targets, dates = etf_for(41)
    shock = 104
    panel, f, caps = shocked_panel(41, targets, {shock: k})
    r = run(panel, f, caps, top10=etf)
    d = r.daily
    T = dates[shock + 1]
    assert (shock + 1 - 61) % 5 != 0                                     # 隔天不是排程再平衡日
    assert d.loc[dates[shock], "ap_min"] < 0.22                          # 衝擊日 Active Share 被打下來
    assert bool(d.loc[T, "rebalanced"]) and d.loc[T, "n_orders"] > 0     # 隔天硬規則：不等 5 日週期，當天再平衡
    assert d.loc[T, "ap_min"] >= 0.22
    assert max_consecutive((d["ap_min"] < 0.20).to_numpy()) <= 1
    assert r.violations == {"holdings": 0, "weight": 0, "cash": 0}


@pytest.mark.parametrize("seed,sign", [(41, 1.1), (41, 0.9), (42, 1.1), (43, 1.1), (43, 0.9)])
def test_limit_up_limit_down_streak_never_two_consecutive_days_below_20(seed, sign):
    etf, targets, dates = etf_for(seed)
    panel, f, caps = shocked_panel(seed, targets, {105 + j: sign for j in range(8)})   # 連續 8 日漲停／跌停
    r = run(panel, f, caps, top10=etf)
    d = r.daily
    assert max_consecutive((d["ap_min"] < 0.20).to_numpy()) <= 1
    assert r.violations == {"holdings": 0, "weight": 0, "cash": 0}
