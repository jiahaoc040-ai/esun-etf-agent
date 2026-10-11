#!/usr/bin/env python
"""Active Share 參數校準（1005 版辦法：前 15 大、< 15% 違規、連 2 日取消資格）。

對每個候選 ap_target（trigger = target − 5pt、recovery = target + 7pt，與 FinalParams 預設的間距相同），
用隨機 tilt 模擬（模擬 Agent 雜訊）逐日檢查 Active Share，回報：
  - 連續 2 日 < 15% 的模擬次數（必須 = 0；取滿足此條件的最小 target）
  - 單日 < 15% 的天數合計、全期間最低值
  - 年換手、樣本外報酬（平均）
  python scripts/calibrate_active_share.py                       # 輸出 docs/active_share_calibration.md
ETF 資料集：
  legacy  data/etf_top15/ 的檔案；若檔案是舊格式（每檔只有前 10 大，標註 "note": LEGACY）則每檔只有 10 檔持股，
          對 15 大口徑而言重疊被低估（樂觀）。
  stress  壓力版：legacy 每檔補上 5 檔「我方基準也最重的大型股」（市值最大、尚未在清單內），權重 = 第 10 名 × 0.95…0.75。
          真實的 ETF 第 11–15 大多半是大型股，重疊會比 legacy 高，stress 是保守估計。
資料日期一旦用 Basic0007B 重抓成真實前 15 大，請重跑本腳本確認結論（見 docs/cloud_tasks.md）。
"""
import argparse
import sys
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.backtest import run_backtest  # noqa: E402
from esun_agent.config import ACTIVE_SHARE_MIN, ROOT  # noqa: E402
from esun_agent.data.etf_holdings import load_top15  # noqa: E402
from esun_agent.data.prices import load_panel  # noqa: E402
from esun_agent.strategy.factors import compute_factors  # noqa: E402
from esun_agent.strategy.final import FinalParams, FinalStrategy, random_tilt_fn  # noqa: E402
from esun_agent.strategy.marketcap import market_cap  # noqa: E402

OOS_START = "2026-08-01"
G: dict = {}


def stress_extend(top: dict[str, dict[str, float]], big: list[str], n_total: int = 15) -> dict[str, dict[str, float]]:
    out = {}
    for etf, h in top.items():
        h = dict(h)
        last = min(h.values())
        extra = [t for t in big if t not in h][:max(0, n_total - len(h))]
        for i, t in enumerate(extra):
            h[t] = round(last * (0.95 - 0.05 * i), 6)
        out[etf] = h
    return out


def params_for(target: float) -> FinalParams:
    return FinalParams(ap_target=round(target, 4), ap_trigger=round(target - 0.05, 4),
                       ap_recovery_target=round(target + 0.07, 4))


def one(args):
    dataset, target, seed, period = args
    r = run_backtest(G["panel"], FinalStrategy(params=params_for(target)), G["factors"], G["top"][dataset],
                     caps=G["caps"], tilt_fn=random_tilt_fn(seed, period=period))
    d = r.daily
    bad = (d["ap_min"] < ACTIVE_SHARE_MIN).to_numpy()
    run_len = best = 0
    for x in bad:
        run_len = run_len + 1 if x else 0
        best = max(best, run_len)
    o = r.summary(OOS_START, None)
    return {"dataset": dataset, "target": target, "period": period, "seed": seed, "consec": best,
            "bad_days": int(bad.sum()), "ap_min": float(d["ap_min"].min()), "turn": o["turnover_ann"],
            "ret": o["total_return"], "rebal": int(d["rebalanced"].sum())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="0.16,0.17,0.18,0.19,0.20,0.22,0.24")
    ap.add_argument("--sims", type=int, default=100)
    ap.add_argument("--stress-sims", type=int, default=30)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", default=str(ROOT / "docs" / "active_share_calibration.md"))
    a = ap.parse_args(argv)
    targets = [float(x) for x in a.targets.split(",")]

    panel = load_panel()
    caps, _ = market_cap(panel)
    legacy = load_top15()
    big = list(caps.iloc[-1].dropna().sort_values(ascending=False).index[:30])
    G.update(panel=panel, factors=compute_factors(panel), caps=caps,
             top={"legacy": legacy, "stress": stress_extend(legacy, big)})
    jobs = [(ds, t, s if p == 5 else 1000 + s, p) for ds in ("legacy", "stress") for t in targets
            for p, n in ((5, a.sims), (1, a.stress_sims)) for s in range(n)]
    with Pool(a.jobs) as pool:
        df = pd.DataFrame(pool.map(one, jobs, chunksize=4))

    L = [f"# Active Share 參數校準（紅線 {ACTIVE_SHARE_MIN:.0%}，前 15 大）\n",
         "方法：每個候選 target（trigger = target − 5pt、recovery = target + 7pt）各跑隨機 tilt 模擬"
         f"（每 5 日重抽 × {a.sims} 次；每日重抽壓力測試 × {a.stress_sims} 次），逐日檢查 Active Share（兩種權重口徑取小、對所有主動型 ETF 取最小）。"
         "樣本 = 2025-04 起至 2026-10 的回測全期間。**「連續 2 日 < 15%」次數 = 0 是硬條件**，單日 < 15% 天數與換手是成本面參考。\n",
         "ETF 資料集：legacy = 目前 repo 內的舊格式快照（每檔 ETF 只有前 10 大，重疊被低估）；"
         "stress = 每檔補 5 檔大型股成前 15 大的保守版（重疊偏高）。真實前 15 大資料需用 Basic0007B 重抓後重跑本腳本。\n"]
    chosen = {}
    for ds in ("legacy", "stress"):
        L.append(f"## 資料集：{ds}\n")
        L.append("| target | 模擬 | 連續 2 日 < 15% 次數 | 單日 < 15% 天數（合計） | 全期間最低值 | 年換手（平均） | 樣本外報酬（平均） | 平均交易日 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for t in targets:
            for p, lab in ((5, "每 5 日 tilt"), (1, "每日 tilt")):
                s = df[(df.dataset == ds) & (df.target == t) & (df.period == p)]
                L.append(f"| {t:.0%} | {lab} ×{len(s)} | {int((s.consec >= 2).sum())} | {int(s.bad_days.sum())} | "
                         f"{s.ap_min.min():.1%} | {s.turn.mean():.1f}x | {s.ret.mean() * 100:+.1f}% | {s.rebal.mean():.0f} |")
        ok = [t for t in targets if int((df[(df.dataset == ds) & (df.target == t)].consec >= 2).sum()) == 0]
        chosen[ds] = min(ok) if ok else None
        L.append(f"\n**{ds}：連續 2 日 < 15% 次數 = 0 的最小 target = "
                 f"{'無（候選皆有違規）' if chosen[ds] is None else format(chosen[ds], '.0%')}**\n")
    Path(a.out).write_text("\n".join(L), encoding="utf-8")
    df.to_csv(Path(a.out).with_suffix(".csv"), index=False)
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    sys.exit(main())
