#!/usr/bin/env python
"""T3 回測：參數組合比較（樣本內選參、樣本外檢驗）＋ 24 個交易日窗口報酬分布。

  python scripts/run_backtest.py            # 輸出 docs/backtest_T3.md
樣本內：T ≤ 2026-06-30（選參只看這段）；樣本外：T ≥ 2026-07-01。
"""
import argparse
import itertools
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.backtest import (Strategy, equal_weight_benchmark, perf, run_backtest,  # noqa: E402
                                 window_returns)
from esun_agent.config import ROOT  # noqa: E402
from esun_agent.data.etf_holdings import load_top10  # noqa: E402
from esun_agent.data.prices import load_panel  # noqa: E402
from esun_agent.strategy.factors import compute_factors  # noqa: E402
from esun_agent.strategy.portfolio import PortfolioParams  # noqa: E402

IS_END = "2026-06-30"
OOS_START = "2026-07-01"

PROFILES = {
    "mom": {"mom20": 0.5, "mom60": 0.5},
    "mom_lowvol": {"mom20": 0.35, "mom60": 0.35, "vol20": -0.3},
    "mom_flow": {"mom20": 0.3, "mom60": 0.3, "foreign20": 0.2, "trust20": 0.2},
    "multi": {"mom5": -0.1, "mom20": 0.25, "mom60": 0.25, "vol20": -0.1, "foreign20": 0.15,
              "trust20": 0.15, "valchg": 0.1},
    "flow": {"foreign20": 0.5, "trust20": 0.5},
}
GRID = {"n_hold": [24, 27], "keep_rank_buffer": [3, 8]}


def pct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.{d}f}%"


def build_strategies() -> list[Strategy]:
    out = []
    for prof, (n, k) in itertools.product(PROFILES, itertools.product(GRID["n_hold"], GRID["keep_rank_buffer"])):
        p = PortfolioParams(n_hold=n, keep_rank_buffer=k)
        out.append(Strategy(f"{prof}|n{n}|k{k}", PROFILES[prof], p))
    return out


def dist_table(df: pd.DataFrame) -> str:
    r = df["ret"]
    q = r.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
    rows = [("窗口數", f"{len(df)}"), ("平均報酬", pct(r.mean())), ("中位數", pct(q[0.5])),
            ("標準差", pct(r.std())), ("5% / 95% 分位", f"{pct(q[0.05])} / {pct(q[0.95])}"),
            ("25% / 75% 分位", f"{pct(q[0.25])} / {pct(q[0.75])}"),
            ("最差 / 最佳", f"{pct(r.min())} / {pct(r.max())}"), ("報酬 > 0 比例", pct((r > 0).mean(), 0)),
            ("勝過等權基準比例", pct((r > df['bench_ret']).mean(), 0)),
            ("基準平均報酬", pct(df["bench_ret"].mean())), ("平均 MDD", pct(df["mdd"].mean())),
            ("違規總次數", f"{int(df['viol'].sum())}")]
    return "| 指標 | 值 |\n|---|---|\n" + "\n".join(f"| {a} | {b} |" for a, b in rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "backtest_T3.md"))
    ap.add_argument("--window-step", type=int, default=3)
    a = ap.parse_args(argv)

    panel = load_panel()
    factors = compute_factors(panel)
    top10 = load_top10()
    strategies = build_strategies()
    print(f"{len(strategies)} 組參數 …", file=sys.stderr)

    results = {}
    for s in strategies:
        r = run_backtest(panel, s, factors, top10)
        results[s.name] = r
        print(s.name, pct(r.summary(None, IS_END)["total_return"]), file=sys.stderr)

    rows = []
    for name, r in results.items():
        i, o = r.summary(None, IS_END), r.summary(OOS_START, None)
        rows.append({"name": name, "i": i, "o": o, "viol": sum(r.violations.values()), "rej": r.rejected_buys})
    best = max(rows, key=lambda x: x["i"]["sharpe"])  # 只看樣本內
    bstrat = next(s for s in strategies if s.name == best["name"])
    first = results[best["name"]].nav.index[1]

    bench_is = perf(equal_weight_benchmark(panel, first, IS_END))
    bench_oos = perf(equal_weight_benchmark(panel, OOS_START, None))
    bench_all = perf(equal_weight_benchmark(panel, first, None))
    dates = panel.dates
    L = []
    L.append("# T3 回測結果\n")
    L.append(f"- 資料：data/market、data/institutional（{dates[0]} ～ {dates[-1]}，{len(dates)} 個交易日，150 檔）。"
             f"因子需 61 個交易日暖機，首個可交易日 {first}。")
    L.append(f"- 樣本內 T ≤ {IS_END}；樣本外 T ≥ {OOS_START}（調參時完全不看）。起始資金 10 億，"
             "每日依流程以 derive_orders 下單、T 日均價成交、扣手續費與證交稅、NAV 用 T 日收盤；價格因子用還原價。")
    L.append(f"- 參數網格：{len(PROFILES)} 組因子權重 × n_hold {GRID['n_hold']} × keep_rank_buffer "
             f"{GRID['keep_rank_buffer']} = **{len(strategies)} 組**；選參準則 = 樣本內 Sharpe。\n")
    L.append("## 1. 參數組合比較\n")
    L.append("| 組合 | IS 報酬 | IS Sharpe | IS MDD | IS 年換手 | OOS 報酬 | OOS Sharpe | OOS MDD | 違規 | 拒單 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for x in sorted(rows, key=lambda x: -x["i"]["sharpe"]):
        i, o = x["i"], x["o"]
        mark = " ⭐" if x["name"] == best["name"] else ""
        L.append(f"| {x['name']}{mark} | {pct(i['total_return'])} | {i['sharpe']:.2f} | {pct(i['mdd'])} | "
                 f"{i['turnover_ann']:.1f}x | {pct(o['total_return'])} | {o['sharpe']:.2f} | {pct(o['mdd'])} | "
                 f"{x['viol']} | {x['rej']} |")
    L.append(f"| 等權基準（150 檔每日再平衡） | {pct(bench_is['total_return'])} | {bench_is['sharpe']:.2f} | "
             f"{pct(bench_is['mdd'])} | — | {pct(bench_oos['total_return'])} | {bench_oos['sharpe']:.2f} | "
             f"{pct(bench_oos['mdd'])} | — | — |")
    L.append("\n年換手 = 日均(買進額+賣出額)/2/NAV × 252。違規 = 持股檔數(20–30)、權重上限(10%/2330 25%)、現金(0–25%) 逐日期末檢查的總次數。\n")

    r = results[best["name"]]
    L.append(f"## 2. 選定組合 `{best['name']}`：樣本內 vs 樣本外\n")
    L.append("| 區間 | 天數 | 報酬 | 年化 | 年化波動 | Sharpe | MDD | 年換手 | 平均持股 | 持股/權重/現金違規 | Active Share 不足天數 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for lab, st, en in (("樣本內", None, IS_END), ("樣本外", OOS_START, None), ("全期間", None, None)):
        s = r.summary(st, en)
        L.append(f"| {lab} | {s['days']} | {pct(s['total_return'])} | {pct(s['ann_return'])} | {pct(s['ann_vol'])} | "
                 f"{s['sharpe']:.2f} | {pct(s['mdd'])} | {s['turnover_ann']:.1f}x | {s['avg_holdings']:.1f} | "
                 f"{s['viol_holdings']}/{s['viol_weight']}/{s['viol_cash']} | {s['ap_fail_days']} |")
    for lab, b in (("等權基準·樣本內", bench_is), ("等權基準·樣本外", bench_oos), ("等權基準·全期間", bench_all)):
        L.append(f"| {lab} | {b['days']} | {pct(b['total_return'])} | {pct(b['ann_return'])} | {pct(b['ann_vol'])} | "
                 f"{b['sharpe']:.2f} | {pct(b['mdd'])} | — | — | — | — |")
    d = r.daily
    L.append(f"\n全期間費用：手續費 {d['fee'].sum() / 1e6:.1f}M、證交稅 {d['tax'].sum() / 1e6:.1f}M；"
             f"停牌未成交委託 {r.unfilled} 筆、因賣單未成交而被拒的買單 {r.rejected_buys} 筆。\n")

    L.append("## 3. 初賽長度（24 個交易日）報酬分布\n")
    L.append(f"每 {a.window_step} 個交易日起算一段、從 10 億現金重新建倉的 24 日回測（含建倉成本），策略 `{best['name']}`。\n")
    w_is = window_returns(panel, bstrat, factors, top10, 24, a.window_step, last=IS_END)
    w_oos = window_returns(panel, bstrat, factors, top10, 24, a.window_step, first=OOS_START)
    L.append("**起點與終點皆在樣本內**\n\n" + dist_table(w_is) + "\n")
    L.append("**起點在樣本外（2026-07-01 起）**\n\n" + dist_table(w_oos) + "\n")

    n = len(strategies)
    L.append(f"## 4. 過度擬合風險\n")
    L.append(f"""1. **選擇偏誤（最大風險）**：150 檔名單是 2026 年才定下的，名單內股票平均漲幅極大（等權基準樣本內 {pct(bench_is['total_return'])}），這段期間的絕對報酬主要反映「事後被選進名單的強勢股」與 AI 供應鏈行情，**不能外推到初賽**。看策略價值應看相對等權基準的超額，而不是絕對報酬。
2. **多重比較**：試了 {n} 組參數再挑樣本內最好的，被挑中的組合其樣本內績效必然偏樂觀；上表可看樣本內排名與樣本外排名是否一致——若不一致，代表排名多半是雜訊。
3. **樣本太短、單一市場狀態**：約 {len(dates) - 61} 個可交易日、一個多頭循環，沒有經歷過長空頭；動能因子在趨勢反轉（急殺後反彈）時會同時重挫，樣本內 MDD 低估了這個風險。樣本外只有約 {int(r.summary(OOS_START, None)['days'])} 天，Sharpe 的標準誤很大。
4. **動能因子本身擁擠**：主動型 ETF 都重壓同一批強勢股，我們又要與它們的前 10 大保持 ≥20% Active Share，策略可能被迫偏離最強的標的。
5. **模型假設**：成交一律以當日均價、無市場衝擊（10 億規模下單檔 ≈ 4,000 萬，已用 20 日均成交值 ≥ 2 億的流動性門檻限制）；不含股利（官方期末加回，回測偏低估）；Active Share 用的是 2026-10-06 的 ETF 前 10 大靜態快照，歷史上各 ETF 持股不同，這項限制在回測中是近似的。
6. **24 日窗口彼此高度重疊**（每 {a.window_step} 日一個、每段 24 日），有效獨立樣本遠少於窗口數，分布的尾部（5%/95%）不可靠。
7. **建議**：把「超額報酬」「換手」「違規次數 = 0」當成上線門檻，不要以回測報酬調參；上線後用實盤前 22 天的結果當第二次樣本外檢驗，達不到再調整會變成另一輪過度擬合。
""")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(L), encoding="utf-8")
    print(f"寫入 {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
