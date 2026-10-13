"""每日候選池：從 150 檔用資料篩出約 30 檔，交給 LLM 挑 10–12 檔高信心部位（回測時以分數前 11 名代理 LLM）。

分數（只用決策日 D 收盤後已公布的資料；橫斷面 z-score，±3 截尾，只在合格名單內標準化）：
  + w_trust5  × z(inst5)    投信近 5 日淨買超金額 ÷ 近 5 日成交值（成交值標準化）
  + w_trust10 × z(inst10)   同上，近 10 日
  + w_rs20    × z(rs20)     20 日相對強度（20 日報酬 − 當日 150 檔中位數）
  + w_valchg  × z(valchg)   成交值放大：log(近 5 日均成交值 ÷ 近 20 日均成交值)
  − overlap_penalty × (出現在幾檔主動型 ETF 前 15 大 ÷ 有資料的 ETF 檔數)   差異化：越多主動型 ETF 重押的股票扣越多
合格：D 日有收盤價、因子齊全、20 日均成交值 ≥ min_adv（單檔 9% × 10 億 ≈ 9000 萬，≤ 日成交值的 30%）。
現有高信心持股即使跌出前 n_candidates 名也會列入（in_pool=False、held=True），讓 LLM 能決定續抱或出場；
2330 一律列入（forced=True），因為重疊扣分會讓它永遠排不進前 30 名。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..active_share import top_n
from .factors import zscore

CANDIDATE_FACTORS = ["inst5", "inst10", "rs20", "valchg"]


@dataclass(frozen=True)
class CandidateParams:
    n_candidates: int = 30
    min_adv: float = 3e8
    w_trust5: float = 0.20
    w_trust10: float = 0.20
    w_rs20: float = 0.30
    w_valchg: float = 0.15
    overlap_penalty: float = 1.0           # z 分數單位：出現在所有主動型 ETF 前 15 大 → 扣 1.0
    always_include: tuple = ("2330",)      # 一律列入候選表（in_pool=False、forced=True）：2330 幾乎在每檔主動型 ETF 前 15 大，
                                           # 重疊扣分會讓它永遠進不了前 30 名，但宣告允許 LLM 選它（上限 20%）


def etf_overlap_counts(top10: dict[str, dict[str, float]] | None) -> tuple[dict[str, int], int]:
    """{ticker: 出現在幾檔主動型 ETF 的前 N 大}，以及有資料的 ETF 檔數。"""
    counts: dict[str, int] = {}
    for h in (top10 or {}).values():
        for t in top_n(h):
            counts[t] = counts.get(t, 0) + 1
    return counts, len(top10 or {})


def screen(fdf: pd.DataFrame, top10: dict[str, dict[str, float]] | None, params: CandidateParams | None = None,
           held: set[str] | frozenset = frozenset()) -> pd.DataFrame:
    """fdf = factors_on(factors, D)。回傳候選表（index = ticker，依 score 由高到低），欄位：
    score、rank（合格名單內的名次，1 起）、inst5、inst10、rs20、valchg、adv20、overlap_n、overlap_frac、in_pool、held、forced。
    只包含前 n_candidates 名、held 中仍合格者，以及 always_include（2330）。"""
    p = params or CandidateParams()
    ok = fdf[CANDIDATE_FACTORS + ["adv20"]].notna().all(axis=1) & (fdf["adv20"] >= p.min_adv)
    sub = fdf.loc[ok, CANDIDATE_FACTORS + ["adv20"]].copy()
    if sub.empty:
        return sub.assign(score=[], rank=[], overlap_n=[], overlap_frac=[], in_pool=[], held=[], forced=[])
    counts, n_etf = etf_overlap_counts(top10)
    sub["overlap_n"] = [counts.get(t, 0) for t in sub.index]
    sub["overlap_frac"] = sub["overlap_n"] / n_etf if n_etf else 0.0
    sub["score"] = (p.w_trust5 * zscore(sub["inst5"]) + p.w_trust10 * zscore(sub["inst10"])
                    + p.w_rs20 * zscore(sub["rs20"]) + p.w_valchg * zscore(sub["valchg"])
                    - p.overlap_penalty * sub["overlap_frac"])
    sub = sub.sort_values(["score"], ascending=False, kind="mergesort")
    sub["rank"] = range(1, len(sub) + 1)
    sub["in_pool"] = sub["rank"] <= p.n_candidates
    sub["held"] = [t in held for t in sub.index]
    sub["forced"] = [t in p.always_include for t in sub.index]
    return sub[sub["in_pool"] | sub["held"] | sub["forced"]]


def proxy_picks(cands: pd.DataFrame, n: int = 11, n_high: int = 5) -> list[tuple[str, str]]:
    """回測用的 LLM 代理：候選池分數前 n 名，前 n_high 名為「高」、其餘為「中」。回傳依優先序排列的 [(ticker, level)]。"""
    pool = cands[cands["in_pool"]].sort_values("rank")
    return [(t, "high" if i < n_high else "medium") for i, t in enumerate(pool.index[:n])]
