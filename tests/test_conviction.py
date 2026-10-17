"""集中主題＋事件驅動：候選池、權重換算、換股紀律、事件前一日、代理回測。"""
import copy
import json

import numpy as np
import pandas as pd
import pytest

from esun_agent import conviction_backtest as cb
from esun_agent.config import ACTIVE_SHARE_MIN
from esun_agent.data.etf_holdings import check_target_weights
from esun_agent.strategy import candidates as ca
from esun_agent.strategy import conviction as cv
from esun_agent.strategy import factors as fa
from esun_agent.strategy import marketcap as mc
from esun_agent.strategy.declaration import DECLARATION_PATH, load_declaration, validate_declaration

from .helpers_market import make_panel

TICKERS = ["2330"] + [str(1001 + i) for i in range(59)]
P = cv.ConvictionParams(min_adv=0.0)
C = ca.CandidateParams(min_adv=0.0)


@pytest.fixture(autouse=True)
def synthetic_universe(monkeypatch):
    monkeypatch.setattr(cv, "load_universe", lambda: {t: {"name": t, "market": "TWSE"} for t in TICKERS})


@pytest.fixture(scope="module")
def syn():
    panel, dates, _ = make_panel(seed=7, n_tickers=60, n_days=140)
    f = fa.compute_factors(panel)
    caps, _ = mc.market_cap(panel, shares=pd.Series(np.arange(60, 0, -1) * 1e7, index=panel.close.columns))
    return panel, dates, f, caps


def cap_series(n=60):
    return pd.Series({t: float(n - i) for i, t in enumerate(TICKERS[:n])})


def picks_of(names, n_high=5):
    return {t: ("high" if i < n_high else "medium") for i, t in enumerate(names)}


# ------------------------------------------------------------------ 候選池
def test_screen_scores_penalizes_etf_overlap_and_includes_held_and_2330(syn):
    panel, dates, f, caps = syn
    fdf = fa.factors_on(f, dates[100])
    base = ca.screen(fdf, None, C)
    assert len(base) == C.n_candidates + int("2330" not in set(base.index[:C.n_candidates]))
    assert list(base["rank"][base["in_pool"]]) == list(range(1, C.n_candidates + 1))
    assert base["score"][base["in_pool"]].is_monotonic_decreasing
    top = base.index[0]
    etfs = {f"E{i}": {top: 0.1, **{t: 0.1 for t in TICKERS[50:59]}} for i in range(5)}
    pen = ca.screen(fdf, etfs, C)
    assert pen.at[top, "overlap_n"] == 5 if top in pen.index else True
    full = ca.screen(fdf, etfs, ca.CandidateParams(min_adv=0.0, n_candidates=60))
    assert full.at[top, "score"] == pytest.approx(base.at[top, "score"] - C.overlap_penalty)
    worst = ca.screen(fdf, None, ca.CandidateParams(min_adv=0.0, n_candidates=60)).index[-1]
    held = ca.screen(fdf, None, C, held={worst})
    assert worst in held.index and bool(held.at[worst, "held"]) and not bool(held.at[worst, "in_pool"])
    assert "2330" in held.index and bool(held.at["2330", "forced"])
    picks = ca.proxy_picks(base, 11, 5)
    assert [t for t, _ in picks] == list(base[base["in_pool"]].index[:11])
    assert [lv for _, lv in picks] == ["high"] * 5 + ["medium"] * 6


def test_screen_respects_liquidity(syn):
    panel, dates, f, caps = syn
    fdf = fa.factors_on(f, dates[100])
    cut = float(fdf["adv20"].median())
    s = ca.screen(fdf, None, ca.CandidateParams(min_adv=cut, n_candidates=60, always_include=()))
    assert (s["adv20"] >= cut).all()


# ------------------------------------------------------------------ 權重換算
def test_levels_to_weights_fillers_and_cash():
    names = TICKERS[30:41]                                         # 11 檔：5 高 6 中 = 73%
    r = cv.build_portfolio(picks_of(names), cap_series(), set(TICKERS), None, P)
    assert r.errors == []
    assert all(r.weights[t] == pytest.approx(P.w_high) for t in names[:5])
    assert all(r.weights[t] == pytest.approx(P.w_mid) for t in names[5:])
    assert P.n_total_min <= r.n_hold <= P.n_total_max and r.cash == pytest.approx(P.cash_target)
    assert r.fillers == TICKERS[:len(r.fillers)]                  # 市值最大者優先、不含高信心名單
    assert all(P.filler_min - 1e-9 <= r.weights[t] <= P.filler_max + 1e-9 for t in r.fillers)
    assert not set(r.fillers) & set(names)


def test_scaling_down_when_all_high_and_up_when_all_medium():
    hi = cv.build_portfolio(picks_of(TICKERS[30:42], n_high=12), cap_series(), set(TICKERS), None, P)
    assert hi.errors == [] and any(n.startswith("conviction_scaled") for n in hi.notes)
    assert max(hi.conviction.values()) < P.w_high and hi.n_hold == P.n_total_min and hi.cash == pytest.approx(P.cash_target)
    lo = cv.build_portfolio(picks_of(TICKERS[30:40], n_high=0), cap_series(), set(TICKERS), None, P)
    assert lo.errors == [] and min(lo.conviction.values()) > P.w_mid and max(lo.conviction.values()) <= P.cap_other + 1e-12
    assert lo.n_hold == P.n_total_max and P.cash_low <= lo.cash <= P.cash_high + 1e-9


def test_tsmc_pick_weights_and_cap():
    r = cv.build_portfolio({"2330": "high", **picks_of(TICKERS[30:40], n_high=3)}, cap_series(), set(TICKERS), None, P)
    assert r.errors == [] and r.weights["2330"] <= P.cap_tsmc + 1e-12 and r.weights["2330"] > P.cap_other
    assert "2330" not in r.fillers


def test_eligibility_and_level_errors():
    r = cv.build_portfolio({"1031": "high", **picks_of(TICKERS[32:42])}, cap_series(), set(TICKERS) - {"1031"}, None, P)
    assert any("1031" in e and "流動性" in e for e in r.errors)
    r = cv.build_portfolio({"1031": "high", **picks_of(TICKERS[32:42])}, cap_series(), set(TICKERS) - {"1031"}, None, P,
                           current={"1031": 0.05})                                      # 已持有 → 可續抱
    assert r.errors == []
    r = cv.build_portfolio({"1031": "low", **picks_of(TICKERS[32:42])}, cap_series(), set(TICKERS), None, P)
    assert any("信心等級" in e for e in r.errors)


def test_active_share_fix_swaps_fillers_then_cuts_conviction():
    names = TICKERS[30:41]
    w0 = cv.build_portfolio(picks_of(names), cap_series(), set(TICKERS), None, P).weights
    fillers0 = [t for t in w0 if t not in names]
    etf = {"E": {**{t: w0[t] for t in names[:8]}, **{t: w0[t] for t in fillers0[:7]}}}   # 前 15 大重疊 8 檔高信心＋7 檔湊檔
    assert check_target_weights(w0, etf, margin=0.0, expected_etfs=["E"])["min"] < ACTIVE_SHARE_MIN
    r = cv.build_portfolio(picks_of(names), cap_series(), set(TICKERS), etf, P)
    assert r.errors == [] and r.active_share["min"] >= P.ap_target - 1e-9
    assert any(n.startswith("ap_conviction_cut") for n in r.notes)
    assert max(r.weights.values()) <= P.cap_other + 1e-12 and P.n_total_min <= r.n_hold <= P.n_total_max
    assert P.cash_low - 1e-9 <= r.cash <= P.cash_high + 1e-9


def test_active_share_unfixable_overlap_is_an_error_for_llm_feedback():
    names = TICKERS[30:41]
    w0 = cv.build_portfolio(picks_of(names), cap_series(), set(TICKERS), None, P).weights
    top = sorted(w0, key=lambda t: -w0[t])[:15]
    etf = {"E": {t: w0[t] for t in top}}                          # 與我方前 15 大完全相同
    r = cv.build_portfolio(picks_of(names), cap_series(), set(TICKERS), etf, P)
    assert any("Active Share" in e and "重疊" in e for e in r.errors)


def test_no_new_freezes_current_weights_and_exits_go_to_cash():
    names = TICKERS[30:41]
    r0 = cv.build_portfolio(picks_of(names), cap_series(), set(TICKERS), None, P)
    cur = {t: w * 1.01 for t, w in r0.weights.items()}
    cur[names[0]] = 0.097                                          # 漂移超過保護線 → 減回配置權重
    new = picks_of(names[1:] + [TICKERS[45]])                     # 想新增 1045（事件前一日不可）
    r = cv.build_portfolio(new, cap_series(), set(TICKERS), None, P, current=cur, no_new=True, exits=[names[0]])
    assert r.errors == [] and TICKERS[45] not in r.weights and names[0] not in r.weights
    assert all(r.weights[t] == pytest.approx(cur[t]) for t in names[1:])
    short = len(cur) - 1 < P.n_total_min                           # 出場後不足下限 → 補 1% 湊檔
    assert any(n.startswith("no_new_filler_added") for n in r.notes) == short and r.n_hold >= P.n_total_min
    assert r.cash == pytest.approx(1 - sum(cur.values()) + cur[names[0]] - (P.filler_min if short else 0.0))
    # 剛好在下限時出場 1 檔 → 必須補湊檔
    cur21 = dict(list(cur.items())[:P.n_total_min])
    r21 = cv.build_portfolio(new, cap_series(), set(TICKERS), None, P, current=cur21, no_new=True, exits=[names[0]])
    assert r21.errors == [] and r21.n_hold == P.n_total_min and any(n.startswith("no_new_filler_added") for n in r21.notes)
    assert "no_new_positions" in r.notes


def test_apply_band_locks_small_moves_and_keeps_cash_in_range():
    target = {t: 0.0475 for t in TICKERS[:20]}                    # 現金 5%
    cur = {t: 0.0475 + (0.005 if i % 2 else -0.005) for i, t in enumerate(TICKERS[:20])}
    out, notes = cv.apply_band(target, cur, P)
    assert out == cur and notes == []
    cur2 = {t: 0.0475 - 0.012 for t in TICKERS[:20]}               # 全部鎖住會讓現金 29% → 逐檔解鎖
    out2, notes2 = cv.apply_band(target, cur2, P)
    assert "band_unlocked_for_cash" in notes2 and P.cash_low <= 1 - sum(out2.values()) <= P.cash_high + 1e-9


# ------------------------------------------------------------------ 換股紀律
def held_map(days, levels=None, ret=None):
    return {t: cv.HeldPick((levels or {}).get(t, "high"), d, (ret or {}).get(t, 0.0)) for t, d in days.items()}


def test_discipline_strict_rules():
    names = TICKERS[30:41]
    held = held_map({t: 5 for t in names} | {names[0]: 1})
    prop = [(t, "high") for t in names[1:]] + [(TICKERS[50], "medium")]
    _, errs, _ = cv.discipline(prop, held, P)
    assert any(names[0] in e and "不可賣出" in e for e in errs)                  # 未滿 3 日不可賣
    prop = [(names[0], "medium")] + [(t, "high") for t in names[1:]]
    _, errs, _ = cv.discipline(prop, held, P)
    assert any("不可降級" in e for e in errs)
    prop = [(t, "high") for t in names[:6]] + [(t, "medium") for t in TICKERS[50:55]]   # 新增 5 檔
    held2 = held_map({t: 5 for t in names})
    _, errs, _ = cv.discipline(prop, held2, P)
    assert any("新增 5 檔" in e for e in errs) and any("移出 5 檔" in e for e in errs)
    _, errs, _ = cv.discipline([(t, "high") for t in TICKERS[40:52]], {}, P)     # 首次建倉不受換股上限
    assert errs == []
    _, errs, _ = cv.discipline([(t, "high") for t in names[:9]], held2 | {}, P)
    assert any("10–12" in e for e in errs)


def test_discipline_stop_loss_and_new_positive_event():
    names = TICKERS[30:41]
    held = held_map({t: 5 for t in names}, ret={names[0]: -0.13})
    prop = [(t, "high") for t in names]
    _, errs, _ = cv.discipline(prop, held, P)
    assert any("停損" in e for e in errs)
    _, errs, _ = cv.discipline(prop, held, P, new_positive={names[0]})
    assert errs == []
    held_new = held_map({t: 5 for t in names} | {names[0]: 1}, ret={names[0]: -0.15})   # 停損優先於最短持有
    _, errs, _ = cv.discipline([(t, "high") for t in names[1:]] + [(TICKERS[50], "high")], held_new, P)
    assert errs == []


def test_discipline_blackout():
    names = TICKERS[30:41]
    held = held_map({t: 5 for t in names}, levels={names[1]: "medium"})
    prop = [(t, "high") for t in names[2:]] + [(names[1], "medium"), (names[0], "high"), (TICKERS[50], "high")]
    _, errs, _ = cv.discipline(prop, held, P, blackout=True)
    assert any("不可新增" in e for e in errs)
    prop = [(t, "high") for t in names]
    _, errs, _ = cv.discipline(prop, held, P, blackout=True)
    assert any("不可升級" in e for e in errs)
    _, errs, _ = cv.discipline([(names[1], "medium")] + [(t, "high") for t in names if t != names[1]], held, P,
                               blackout=True)
    assert errs == []


def test_discipline_repair_mode():
    names = TICKERS[30:41]
    held = held_map({t: 5 for t in names} | {names[0]: 1}, ret={names[1]: -0.2})
    prop = [(t, "high") for t in TICKERS[45:56]]                    # 想全部換掉
    final, errs, notes = cv.discipline(prop, held, P, strict=False)
    assert errs == []
    assert names[0] in final and names[1] not in final              # 鎖定者保留、停損者出場
    added = [t for t in final if t not in held]
    dropped = [t for t in held if t not in final and t != names[1]]
    assert len(added) <= P.max_swaps and len(dropped) <= P.max_swaps
    assert P.n_conv_min <= len(final) <= P.n_conv_max
    final_b, _, notes_b = cv.discipline(prop, held, P, strict=False, blackout=True)
    assert not [t for t in final_b if t not in held] and names[1] not in final_b
    assert set(final_b) == set(held) - {names[1]}                    # 事件前一日：凍結


def test_blackout_calendar():
    ev = [{"date": "2026-10-28", "kind": "fomc", "name": "FOMC"}, {"date": "2026-11-21", "kind": "x", "name": "週六事件", "blackout": True}]
    assert cv.blackout_events("2026-10-28", ev) and not cv.blackout_events("2026-10-27", ev)
    assert cv.blackout_events("2026-11-20", ev)                       # 週五：下一交易日是週一 → 週六事件也算
    assert not cv.blackout_events("2026-10-29", ev)
    assert cv.blackout_events("2026-10-27", ev, next_session="2026-10-29")  # 已知 10/28 休市 → 10/27 是前一交易日
    real = cv.load_event_calendar()
    kinds = {e["kind"] for e in real}
    assert {"fomc", "nvidia_earnings", "us_election"} <= kinds
    assert all(e["date"] >= "2025-01-01" for e in real)
    assert cv.blackout_events("2026-11-03", real)                       # 美國期中選舉


def test_pick_return_uses_entry_avg_price(syn):
    panel, dates, f, caps = syn
    t, e, d = "1005", dates[100], dates[105]
    want = panel.adj_close.at[d, t] / (panel.avg_price.at[e, t] * panel.adj_close.at[e, t] / panel.close.at[e, t]) - 1
    assert cv.pick_return(panel, t, e, d) == pytest.approx(want)
    assert cv.pick_return(panel, t, dates[106], d) is None


# ------------------------------------------------------------------ 代理回測
def test_proxy_backtest_synthetic_never_violates(syn, monkeypatch):
    panel, dates, f, caps = syn
    etf = {"E": {t: 0.1 for t in TICKERS[:10]}}
    r = cb.run_conviction_backtest(panel, f, caps, etf, dates[70], None, params=P, cand_params=C, events=[
        {"date": dates[90], "kind": "fomc", "name": "FOMC"}])
    d = r.daily
    assert r.violations["holdings"] == 0 and r.violations["cash"] == 0 and r.build_errors == 0
    assert d["n_hold"].between(20, 30).all() and (d["ap_min"] >= ACTIVE_SHARE_MIN).all()
    assert d["n_added"].iloc[1:].max() <= P.max_swaps
    assert d.loc[dates[90], "blackout"] == 1 and d.loc[dates[90], "n_added"] == 0
    assert (d["v_weight_buy"] == 0).all()
    s = r.summary()
    assert s["days"] == len(d) and s["cost"] > 0
    for x in r.picks_log:                                              # 名單檔數在 10–12（停損日可少）
        if "picks" in x:
            assert len(x["picks"]) <= P.n_conv_max


def test_proxy_backtest_min_hold(syn):
    panel, dates, f, caps = syn
    r = cb.run_conviction_backtest(panel, f, caps, None, dates[70], dates[100], params=P, cand_params=C, events=[])
    log = [x for x in r.picks_log if "picks" in x]
    entry: dict[str, int] = {}
    for i, x in enumerate(log):
        for t in x["picks"]:
            entry.setdefault(t, i)
        for t in list(entry):
            if t not in x["picks"]:
                assert i - entry[t] >= P.min_hold_days or any(t in y for y in ()), (t, i, entry[t])
                del entry[t]


def test_window_helpers():
    dates = [f"d{i:03d}" for i in range(200)]
    s = cb.window_starts(dates, "d070", 24, 2)
    assert s[0] == 70 and s[-1] + 23 <= 199 and all(b - a == 2 for a, b in zip(s, s[1:]))
    ex = cb.excess_distribution(pd.Series([0.12, 0.06, -0.11, 0.0]))
    assert ex["p_gt_5"] == 0.5 and ex["p_gt_10"] == 0.25 and ex["p_lt_m10"] == 0.25


# ------------------------------------------------------------------ 宣告檔
def test_declaration_v2_matches_code():
    d = load_declaration()
    assert d["version"] == "2.3.1" and d["status"] == "final"
    assert d["portfolio_rules"]["n_total_min"] == 23 and d["portfolio_rules"]["n_total_max"] == 24
    r = d["portfolio_rules"]
    assert (r["w_high"], r["w_mid"], r["cap_other"], r["cap_tsmc"]) == (0.08, 0.055, 0.08, 0.20)
    assert (r["w_tsmc_high"], r["w_tsmc_mid"], r["keep_rank"]) == (0.15, 0.10, 30)
    assert (r["n_conv_min"], r["n_conv_max"], r["min_hold_days"], r["max_swaps"], r["stop_loss"]) == (10, 12, 3, 4, 0.12)
    assert (r["cash_low"], r["cash_high"], r["ap_target"]) == (0.02, 0.05, 0.20)
    assert d["candidate_rules"]["n_candidates"] == 30 and d["fallback"]["name"] == "合規基準 A"
    assert d["fallback"]["portfolio_rules"]["ap_target"] == 0.22


def test_declaration_detects_drift():
    d = json.loads(DECLARATION_PATH.read_text(encoding="utf-8"))
    for section, key, val in (("portfolio_rules", "w_high", 0.1), ("candidate_rules", "w_rs20", 0.5)):
        bad = copy.deepcopy(d)
        bad[section][key] = val
        assert any(key in e for e in validate_declaration(bad))
    bad = copy.deepcopy(d)
    bad["fallback"]["portfolio_rules"]["max_active"] = 0.2
    assert any("max_active" in e for e in validate_declaration(bad))
    bad = copy.deepcopy(d)
    del bad["discipline"]
    assert validate_declaration(bad) == ["缺少欄位 discipline"]


def test_no_new_redeploys_cash_when_many_stop_losses():
    names = TICKERS[30:41]
    r0 = cv.build_portfolio(picks_of(names), cap_series(), set(TICKERS), None, P)
    exits = names[:4]                                              # 同日 4 檔停損（約 33% NAV）
    keep = picks_of([t for t in names if t not in exits])
    r = cv.build_portfolio(keep, cap_series(), set(TICKERS), None, P, current=r0.weights, no_new=True, exits=exits)
    assert r.errors == [] and any(n.startswith("no_new_cash_redeployed") for n in r.notes)
    assert r.cash <= 0.22 and not set(exits) & set(r.weights) and r.n_hold >= P.n_total_min
    added = set(r.weights) - set(r0.weights)                          # 新名稱只可能是為了湊滿下限的湊檔股
    assert added and all(f"no_new_filler_added:{t}" in r.notes for t in added) and not added & set(keep)
    assert all(x <= P.cap(t) + 1e-12 for t, x in r.weights.items())


def test_fillers_target_23_to_24_holdings_with_3_name_buffer():
    for names, n_high in ((TICKERS[30:40], 0), (TICKERS[30:41], 5), (TICKERS[30:42], 12), (TICKERS[30:42], 0)):
        r = cv.build_portfolio(picks_of(names, n_high), cap_series(), set(TICKERS), None, P)
        assert r.errors == [] and 23 <= r.n_hold <= 24, (len(names), n_high, r.n_hold)
        assert r.n_hold - 20 >= 3                                                  # 至少 3 檔緩衝
        assert all(P.filler_min - 1e-9 <= r.weights[t] <= P.filler_max + 1e-9 for t in r.fillers)
    assert P.n_total_min == 23


def test_fillers_prefer_large_cap_with_low_etf_overlap():
    names = TICKERS[30:41]
    heavy = TICKERS[:3]                                                          # 市值前 3 大，出現在所有 ETF 前 15 大
    etfs = {f"E{i}": {**{t: 0.1 for t in heavy}, **{t: 0.05 for t in TICKERS[50:52]}} for i in range(4)}
    order = cv.filler_candidates(cap_series(), set(TICKERS), set(names), P, top15=etfs)
    assert order[:3] == TICKERS[3:6] and order[-3:] == heavy                     # 低重疊依市值在前，高重疊排最後
    assert len(order) == P.filler_pool - len(set(names) & set(TICKERS[:P.filler_pool]))
    r = cv.build_portfolio(picks_of(names), cap_series(), set(TICKERS), etfs, P)
    assert r.errors == [] and not set(heavy) & set(r.fillers)
    # 現有湊檔股：低重疊者保留在前（避免換手）；高重疊者即使現在持有也排到後面
    order2 = cv.filler_candidates(cap_series(), set(TICKERS), set(names), P, current_fillers=[TICKERS[20], heavy[0]], top15=etfs)
    assert order2[0] == TICKERS[20] and order2.index(heavy[0]) >= len(order2) - 3
    # 沒有 ETF 資料：純依市值
    assert cv.filler_candidates(cap_series(), set(TICKERS), set(names), P)[:3] == heavy
