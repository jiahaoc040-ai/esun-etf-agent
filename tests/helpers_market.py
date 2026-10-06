"""合成行情面板（不依賴 data/ 目錄），供策略、回測測試使用。"""
import numpy as np
import pandas as pd

from esun_agent.data.prices import build_panel


def make_panel(n_tickers: int = 40, n_days: int = 120, seed: int = 0, splits: dict | None = None,
               start: str = "2025-01-01", halt: dict | None = None):
    """splits={(ticker, day_idx): k}：該日起價格除以 k（k:1 分割），當日另有隨機漲跌。
    halt={ticker: (a, b)}：a≤day_idx<b 無資料（停牌）。ticker 為 '2330' 與 '1001'…。"""
    rng = np.random.default_rng(seed)
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=n_days)]
    tickers = ["2330"] + [str(1001 + i) for i in range(n_tickers - 1)]
    rows, inst = [], []
    price = {t: 50.0 + 20 * rng.random() for t in tickers}
    for i, d in enumerate(dates):
        for t in tickers:
            if halt and t in halt and halt[t][0] <= i < halt[t][1]:
                continue
            ret = rng.normal(0.0005, 0.02)
            price[t] *= 1 + ret
            k = (splits or {}).get((t, i))
            if k:
                price[t] /= k
            c = round(price[t], 2)
            o = round(c * (1 + rng.normal(0, 0.005)), 2)
            avg = round(c * (1 + rng.normal(0, 0.003)), 4)
            vol = int(rng.integers(2_000_000, 8_000_000))
            rows.append({"date": d, "ticker": t, "open": o, "close": c, "avg_price": avg,
                         "volume": vol, "value": int(vol * avg)})
            inst.append({"date": d, "ticker": t, "foreign_net": float(rng.normal(0, 3e5)),
                         "trust_net": float(rng.normal(0, 1e5)), "dealer_net": 0.0, "total_net": 0.0})
    panel = build_panel(pd.DataFrame(rows), pd.DataFrame(inst))
    panel.volume, panel.value = panel.volume.astype(float), panel.value.astype(float)
    return panel, dates, tickers
