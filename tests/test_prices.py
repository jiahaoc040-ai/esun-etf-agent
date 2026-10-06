import numpy as np
import pandas as pd
import pytest

from esun_agent.data import prices as pr


def close_frame(series: dict[str, list]) -> pd.DataFrame:
    n = max(len(v) for v in series.values())
    idx = [f"2025-01-{i + 1:02d}" for i in range(n)]
    return pd.DataFrame({k: pd.Series(v, index=idx[:len(v)]) for k, v in series.items()}, index=idx)


def test_detect_split_keeps_residual_move():
    c = close_frame({"A": [100, 101, 25.5, 26, 27]})          # 4:1，當日實際 +1%
    ev = pr.detect_splits(c)
    assert [(e.date, e.ticker, e.ratio) for e in ev] == [("2025-01-03", "A", 4.0)]
    adj = c * pr.adjustment_factors(c, ev)
    r = adj["A"].pct_change().dropna()
    assert r.abs().max() < 0.05 and r.iloc[1] == pytest.approx(25.5 * 4 / 101 - 1)
    assert adj["A"].iloc[-1] == 27                              # 最新一段不動


def test_ratio_snaps_to_natural_split():
    c = close_frame({"A": [1215.0, 133.5]})                      # 6919：1/0.1099 ≈ 9.1，應取 10
    assert pr.detect_splits(c)[0].ratio == 10.0


def test_dividend_and_limit_moves_not_flagged():
    c = close_frame({"A": [246.5, 212.5, 200.0], "B": [100, 90, 99], "C": [100, 78.5, 80]})  # 最多 −21.5%
    assert pr.detect_splits(c) == []


def test_reverse_split_and_halt_gap():
    c = close_frame({"A": [10, 10.2, np.nan, np.nan, np.nan, 40.8]})   # 停牌後 4 合 1，價格 ×4
    ev = pr.detect_splits(c)
    assert len(ev) == 1 and ev[0].ratio == 0.25
    adj = (c * pr.adjustment_factors(c, ev))["A"].dropna()
    assert adj.iloc[-1] == pytest.approx(40.8) and adj.iloc[0] == pytest.approx(40.0)


def test_real_data_events_match_known_splits():
    panel = pr.load_panel()
    got = {(e.date, e.ticker): e.ratio for e in panel.events}
    assert got == {("2025-07-21", "6919"): 10.0, ("2025-08-25", "2327"): 4.0,
                   ("2026-03-09", "8932"): 2.0, ("2026-09-02", "6669"): 3.0}
    r = panel.adj_close.ffill().pct_change()
    for (d, t) in got:
        assert abs(r.loc[d, t]) <= 0.11          # 還原後不再有跳空
    assert panel.close.loc["2026-09-02", "6669"] < 3000  # 原始價仍是未還原
