"""Active Share 檢查（比賽辦法 六(十三)）。

  Active Share = 1/2 × Σ |Wi(投資組合) − Wi(Benchmark)|
比較對象：「我方持股前 10 大」vs「每一檔主動型 ETF 的前 10 大持股」，任一檔 < 20% 且連續 2 日 → 可能取消資格。

辦法沒寫清楚權重口徑，因此兩種都算、取較嚴（較小）者：
  - normalized：兩邊前 10 大各自重新歸一到 100%（最常見的算法）
  - raw：直接用占淨值比例
ETF 前 10 大持股需另外抓（投信官網/月報，authority=fininst），格式 {etf_code: {ticker: weight}}。
"""
from .config import ACTIVE_SHARE_MIN, ACTIVE_SHARE_TOP_N


def top_n(weights: dict[str, float], n: int = ACTIVE_SHARE_TOP_N) -> dict[str, float]:
    return dict(sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))[:n])


def _normalize(w: dict[str, float]) -> dict[str, float]:
    s = sum(w.values())
    return {k: v / s for k, v in w.items()} if s else w


def active_share(portfolio: dict[str, float], benchmark: dict[str, float], normalize: bool = True) -> float:
    p, b = top_n(portfolio), top_n(benchmark)
    if normalize:
        p, b = _normalize(p), _normalize(b)
    keys = set(p) | set(b)
    return 0.5 * sum(abs(p.get(k, 0.0) - b.get(k, 0.0)) for k in keys)


def check_active_share(portfolio: dict[str, float], etf_top10: dict[str, dict[str, float]],
                       threshold: float = ACTIVE_SHARE_MIN) -> dict:
    """回傳 {"ok": bool, "min": float, "worst_etf": str, "detail": {etf: {"normalized":..,"raw":..}}}"""
    detail, worst, worst_val = {}, None, float("inf")
    for etf, holdings in etf_top10.items():
        n = active_share(portfolio, holdings, normalize=True)
        r = active_share(portfolio, holdings, normalize=False)
        detail[etf] = {"normalized": round(n, 6), "raw": round(r, 6)}
        if min(n, r) < worst_val:
            worst, worst_val = etf, min(n, r)
    return {"ok": worst_val >= threshold, "min": worst_val if worst else None, "worst_etf": worst, "detail": detail}
