#!/usr/bin/env python
"""T3 回測（核心＋衛星）：8 組參數比較、樣本內相對排序、樣本外檢驗、24 日窗口超額報酬分布。

  python scripts/run_backtest.py            # 輸出 docs/backtest_T3.md
評估口徑：150 檔名單是 2026-07 底以市值選出，2026-08-01 之前有前視／倖存者偏誤。
  樣本內 T ≤ 2026-07-31 只用來比較參數的「相對」排序；樣本外 T ≥ 2026-08-01 才是績效檢驗。
  所有結果都列相對兩個基準的超額：(a) 150 檔等權；(b) 市值加權並套上限（2330 ≤ 20%、其他 ≤ 8%）。
"""
import argparse
import itertools
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.backtest import (cap_weight_benchmark, equal_weight_benchmark, perf, run_backtest,  # noqa: E402
                                 window_returns)
from esun_agent.config import ROOT  # noqa: E402
from esun_agent.data.etf_holdings import load_top10  # noqa: E402
from esun_agent.data.prices import load_panel  # noqa: E402
from esun_agent.strategy.core_satellite import CoreSatellite, CoreSatelliteParams  # noqa: E402
from esun_agent.strategy.factors import compute_factors  # noqa: E402
from esun_agent.strategy.marketcap import market_cap  # noqa: E402

IS_END = "2026-07-31"
OOS_START = "2026-08-01"

SAT_PROFILES = {
    "equal": (("inst10", 1.0), ("mom20s5", 1.0), ("vol20", -1.0)),
    "trust": (("inst10", 2.0), ("mom20s5", 1.0), ("vol20", -1.0)),
}
TOTAL_HOLD = 28      # core_k + sat_n 固定 28 檔（官方 20–30，留緩衝）
GRID = {"sat_n": [6, 10], "profile": list(SAT_PROFILES), "keep_rank_buffer": [3, 10]}   # 2×2×2 = 8 組


def pct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:+.{d}f}%"


def ppct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.{d}f}%"


def build_strategies() -> list[CoreSatellite]:
    out = []
    for n, prof, kb in itertools.product(GRID["sat_n"], GRID["profile"], GRID["keep_rank_buffer"]):
        p = CoreSatelliteParams(core_k=TOTAL_HOLD - n, sat_n=n, sat_weights=SAT_PROFILES[prof], keep_rank_buffer=kb)
        out.append(CoreSatellite(f"sat{n}|{prof}|keep{kb}", p))
    return out


def bench_ret(b: pd.Series, base: str, start: str | None, end: str | None) -> float:
    """基準在 (start, end] 區間的報酬；base = 策略首筆 NAV 日（起點）。"""
    lo = b[b.index < start].index[-1] if start else base
    hi = end or b.index[-1]
    hi = b[b.index <= hi].index[-1]
    return float(b[hi] / b[lo] - 1)


def active_ir(nav: pd.Series, bench: pd.Series, start, end) -> float:
    r = nav.pct_change().dropna()
    r = r[(r.index >= (start or r.index[0])) & (r.index <= (end or r.index[-1]))]
    b = bench.pct_change().reindex(r.index)
    a = (r - b).dropna()
    return float(a.mean() / a.std(ddof=1) * np.sqrt(252)) if len(a) > 2 and a.std(ddof=1) > 0 else float("nan")


def dist_table(df: pd.DataFrame) -> str:
    def row(label, col):
        x = df[col]
        q = x.quantile([0.05, 0.5, 0.95])
        return (f"| {label} | {ppct(x.mean())} | {ppct(q[0.5])} | {ppct(x.std())} | {ppct(q[0.05])} / {ppct(q[0.95])} | "
                f"{(x > 0).mean() * 100:.0f}% |")
    lines = ["| 指標 | 平均 | 中位數 | 標準差 | 5% / 95% 分位 | 勝率（>0） |", "|---|---|---|---|---|---|",
             row("策略報酬", "ret"), row("150 檔等權報酬", "bench_ret"), row("市值加權上限報酬", "bench_cw_ret"),
             row("**超額 vs 等權 (a)**", "ex_ew"), row("**超額 vs 市值加權上限 (b)**", "ex_cw")]
    lines.append(f"\n窗口數 {len(df)}；平均 MDD {ppct(df['mdd'].mean())}；違規總次數 {int(df['viol'].sum())}。")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "backtest_T3.md"))
    ap.add_argument("--window-step", type=int, default=2)
    a = ap.parse_args(argv)

    panel = load_panel()
    factors = compute_factors(panel)
    top10 = load_top10()
    caps, cap_src = market_cap(panel)
    strategies = build_strategies()
    dates = panel.dates
    print(f"{len(strategies)} 組參數；市值來源 {cap_src}", file=sys.stderr)

    ew_full = equal_weight_benchmark(panel)
    cw_full = cap_weight_benchmark(panel, caps)
    results = {}
    for s in strategies:
        results[s.name] = run_backtest(panel, s, factors, top10, caps=caps)
        print(s.name, file=sys.stderr)

    first_base = next(iter(results.values())).nav.index[0]
    first_T = next(iter(results.values())).nav.index[1]
    rows = []
    for s in strategies:
        r = results[s.name]
        i, o = r.summary(None, IS_END), r.summary(OOS_START, None)
        rows.append({
            "name": s.name, "i": i, "o": o, "viol": sum(r.violations.values()),
            "ir": active_ir(r.nav, cw_full, None, IS_END),
            "i_ex_ew": i["total_return"] - bench_ret(ew_full, first_base, None, IS_END),
            "i_ex_cw": i["total_return"] - bench_ret(cw_full, first_base, None, IS_END),
            "o_ex_ew": o["total_return"] - bench_ret(ew_full, first_base, OOS_START, None),
            "o_ex_cw": o["total_return"] - bench_ret(cw_full, first_base, OOS_START, None),
        })
    best = max(rows, key=lambda x: (x["ir"] if not np.isnan(x["ir"]) else -9))   # 只看樣本內「相對」表現
    bstrat = next(s for s in strategies if s.name == best["name"])
    br = results[best["name"]]

    b_ew = {k: bench_ret(ew_full, first_base, *v) for k, v in (("is", (None, IS_END)), ("oos", (OOS_START, None)), ("all", (None, None)))}
    b_cw = {k: bench_ret(cw_full, first_base, *v) for k, v in (("is", (None, IS_END)), ("oos", (OOS_START, None)), ("all", (None, None)))}
    ew_perf = {k: perf(equal_weight_benchmark(panel, *v)) for k, v in (("is", (first_T, IS_END)), ("oos", (OOS_START, None)))}
    cw_perf = {k: perf(cap_weight_benchmark(panel, caps, *v)) for k, v in (("is", (first_T, IS_END)), ("oos", (OOS_START, None)))}

    L = ["# T3 回測結果（核心＋衛星）\n"]
    L.append("## 0. 口徑與資料\n")
    L.append(f"- 資料：data/market、data/institutional，{dates[0]} ～ {dates[-1]}（{len(dates)} 個交易日，150 檔）。"
             f"因子需 61 個交易日暖機，首個可交易日 {first_T}。起始資金 10 億；每日流程用 derive_orders 下單、"
             "T 日原始均價成交、扣手續費與證交稅、NAV 用 T 日收盤；價格因子與基準用還原價。")
    L.append(f"- **樣本外 = T ≥ {OOS_START}**（{br.summary(OOS_START, None)['days']} 個交易日）。150 檔名單是 2026-07 底以市值選出，"
             f"**{OOS_START} 之前有前視／倖存者偏誤**：樣本內（T ≤ {IS_END}）只用來比較參數的「相對」排序，"
             "其絕對報酬與超額報酬都不可當成績效。")
    L.append("- 基準：(a) 150 檔等權（每日再平衡）；(b) 市值加權並套上限（2330 ≤ 20%、其他 ≤ 8%，每日再平衡，用前一日市值定權重）。"
             "兩者都不計成本，且同樣帶有名單偏誤，所以「超額」比絕對報酬可信。")
    if cap_src == "issued_shares":
        L.append("- 市值 = 還原收盤價 × 發行股數快照（data/reference/shares_outstanding.csv）。")
    else:
        L.append("- ⚠️ **市值為暫代值（本次結果為暫定）**：沒有發行股數快照，市值以「近 60 日均成交值」代理。"
                 "TPEx 日行情有「發行股數」（已驗證），但 TWSE 日行情 API 沒有；上市股的替代來源是 TWSE OpenAPI `t187ap03_L`"
                 "（已發行普通股數），雲端環境連不到，所以請在本機執行 `python scripts/fetch_shares.py` 產生 "
                 "data/reference/shares_outstanding.csv 後重跑本腳本。均成交值代理會偏重高週轉的熱門股、"
                 "低估 2330 等低週轉權值股（代理下 2330 約 9–10%，真實市值占比應高很多），因此 (b) 基準與核心權重都只是近似。")
    L.append(f"- 策略：核心 65%（市值前 core_k 大、市值加權＋上限 2330 ≤ 20%／其他 ≤ 8%）＋ 衛星 35%（核心以外、依綜合分數取前 sat_n 檔："
             "投信 10 日淨買超(成交值標準化)、20 日動能(略過最近 5 日)、20 日波動度(懲罰)）；core_k + sat_n = 28；現金目標 3%；"
             "**每 5 個交易日再平衡**、**無交易帶 2%**；Active Share 以 check_target_weights（緩衝 2%）修正。")
    L.append(f"- 參數網格 **{len(strategies)} 組** = sat_n {GRID['sat_n']} × 衛星因子權重 {GRID['profile']}（equal = 三因子等權；"
             "trust = 投信因子權重 ×2）× 衛星排名遲滯 {3, 10}；選參準則 = 樣本內相對基準 (b) 的資訊比率（只看相對）。\n")

    L.append("## 1. 8 組參數比較\n")
    L.append("樣本內欄位只用來看相對排序（有前視偏誤）；樣本外是真正的檢驗。超額 = 策略累計報酬 − 基準累計報酬。\n")
    L.append("| 組合 | IS 超額 vs (b) | IS 資訊比率 | IS 年換手 | OOS 報酬 | OOS 超額 vs (a) 等權 | OOS 超額 vs (b) 市值上限 | OOS MDD | OOS 年換手 | 違規 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for x in sorted(rows, key=lambda x: -x["ir"]):
        i, o = x["i"], x["o"]
        mark = " ⭐" if x["name"] == best["name"] else ""
        L.append(f"| {x['name']}{mark} | {pct(x['i_ex_cw'])} | {x['ir']:.2f} | {i['turnover_ann']:.1f}x | "
                 f"{pct(o['total_return'])} | {pct(x['o_ex_ew'])} | {pct(x['o_ex_cw'])} | {pct(o['mdd'])} | "
                 f"{o['turnover_ann']:.1f}x | {x['viol']} |")
    L.append(f"| 基準 (a) 150 檔等權 | — | — | — | {pct(b_ew['oos'])} | — | — | {pct(ew_perf['oos']['mdd'])} | — | — |")
    L.append(f"| 基準 (b) 市值加權上限 | — | — | — | {pct(b_cw['oos'])} | — | — | {pct(cw_perf['oos']['mdd'])} | — | — |")
    L.append("\n年換手 = 日均(買進額+賣出額)/2/NAV × 252。違規 = 持股檔數(20–30)、權重上限(10%/2330 25%)、現金(0–25%) 逐日期末檢查的總次數。\n")

    L.append(f"## 2. 選定組合 `{best['name']}`：樣本內 vs 樣本外\n")
    L.append("| 區間 | 天數 | 策略報酬 | 超額 vs (a) | 超額 vs (b) | 年化波動 | MDD | 年換手 | 年化費用拖累 | 費用占毛利 | 平均持股 | 違規(持股/權重/現金) | Active Share 不足天數 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for lab, st, en, k in (("樣本內（有偏誤，僅看相對）", None, IS_END, "is"), ("**樣本外**", OOS_START, None, "oos"),
                           ("全期間（有偏誤）", None, None, "all")):
        s = br.summary(st, en)
        L.append(f"| {lab} | {s['days']} | {pct(s['total_return'])} | {pct(s['total_return'] - b_ew[k])} | "
                 f"{pct(s['total_return'] - b_cw[k])} | {ppct(s['ann_vol'])} | {pct(s['mdd'])} | {s['turnover_ann']:.1f}x | "
                 f"{ppct(s['cost_drag_ann'])} | {ppct(s['cost_share_of_gross'])} | {s['avg_holdings']:.1f} | "
                 f"{s['viol_holdings']}/{s['viol_weight']}/{s['viol_cash']} | {s['ap_fail_days']} |")
    for lab, b, k in (("基準 (a) 等權", b_ew, "oos"), ("基準 (b) 市值加權上限", b_cw, "oos")):
        L.append(f"| {lab}·樣本外 | — | {pct(b[k])} | — | — | — | — | — | — | — | — | — | — |")
    d = br.daily
    L.append(f"\n全期間手續費 {d['fee'].sum() / 1e6:.1f}M、證交稅 {d['tax'].sum() / 1e6:.1f}M；停牌未成交委託 {br.unfilled} 筆、"
             f"因賣單未成交被拒的買單 {br.rejected_buys} 筆。「費用占毛利」= 費稅 ÷ (損益 + 費稅)。\n")

    L.append("## 3. 初賽長度（24 個交易日）超額報酬分布\n")
    L.append(f"每 {a.window_step} 個交易日起算一段、從 10 億現金重新建倉（含建倉成本與每 5 日再平衡）的 24 日回測，策略 `{best['name']}`。"
             "超額 = 策略該段報酬 − 基準同段報酬。\n")
    w_oos = window_returns(panel, bstrat, factors, top10, 24, a.window_step, first=OOS_START, caps=caps, bench_cw=cw_full)
    w_is = window_returns(panel, bstrat, factors, top10, 24, a.window_step, last=IS_END, caps=caps, bench_cw=cw_full)
    L.append("**起點在樣本外（T ≥ 2026-08-01）——僅此組可信**\n\n" + dist_table(w_oos) + "\n")
    L.append("**樣本內（有偏誤，僅供參考）**\n\n" + dist_table(w_is) + "\n")

    n = len(strategies)
    so = br.summary(OOS_START, None)
    L.append("## 4. 過度擬合風險與限制\n")
    L.append(f"""1. **樣本外太短**：只有 {so['days']} 個交易日、24 日窗口只有 {len(w_oos)} 個且彼此高度重疊（每 {a.window_step} 日一個、每段 24 日），有效獨立樣本接近 1–2 個，勝率與分位數都不可靠。
2. **偏誤已隔離但沒消失**：2026-08-01 之後名單的選擇偏誤較輕，但名單本身仍是以 2026-07 底市值選出，之後新進／退出的公司不在其中；樣本內不能當績效，只能看參數的相對排序。
3. **多重比較**：8 組參數挑樣本內相對最佳；樣本內排名與樣本外排名若不一致，代表排序多半是雜訊（見第 1 節表格）。參數數量已刻意壓低，核心／衛星比例（65/35）、再平衡週期（5 日）、無交易帶（2%）都是預先指定、沒有調。
4. **市值代理**：""" + ("已使用發行股數。" if cap_src == "issued_shares" else
             "目前市值是 60 日均成交值代理，(b) 基準與核心權重因此失真（見第 0 節），**拿到 shares_outstanding.csv 後必須重跑**；"
             "在那之前，超額報酬只能當暫定參考。") + """
5. **核心的實際曝險**：核心只持有市值前 core_k 大，而 (b) 基準涵蓋 150 檔，兩者本來就有追蹤誤差；Active Share 修正若觸發會再壓低與主動型 ETF 重疊的標的、進一步偏離基準（全期間不足天數見第 2 節）。
6. **模型假設**：均價成交、無市場衝擊、不含股利（官方期末加回，回測偏低估）；Active Share 用 2026-10-06 的 ETF 前 10 大靜態快照，歷史上不同，只是近似；衛星因子（投信買超、動能）在擁擠交易反轉時會同時失靈。
7. **上線門檻建議**：以「樣本外超額報酬（兩個基準）為正、年換手與費用拖累可接受、違規 = 0」為準，不要以回測報酬調參；實盤前 22 天是第二次樣本外檢驗。
""")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(L), encoding="utf-8")
    print(f"寫入 {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
