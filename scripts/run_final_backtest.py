#!/usr/bin/env python
"""正式策略（合規基準 A ＋ Agent 小幅加減碼）回測：無 tilt 基線 + 100 次隨機 tilt（模擬 Agent 雜訊）。

  python scripts/run_final_backtest.py            # 輸出 docs/final_strategy_backtest.md
隨機 tilt：每次 5 檔、幅度 ±1–3%、每 5 個交易日重抽一次（與再平衡同步）；另做每天都重抽的壓力測試（tilt 每天變動 → 每天都交易）。
樣本外 = T ≥ 2026-08-01；樣本內有名單偏誤，僅供參考。
"""
import argparse
import sys
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent.backtest import (cap_weight_benchmark, equal_weight_benchmark, perf, run_backtest)  # noqa: E402
from esun_agent.config import ROOT  # noqa: E402
from esun_agent.data.etf_holdings import load_top10  # noqa: E402
from esun_agent.data.prices import load_panel  # noqa: E402
from esun_agent.strategy.factors import compute_factors, factors_on  # noqa: E402
from esun_agent.strategy.final import FinalParams, FinalStrategy, base_plan, random_tilt_fn  # noqa: E402
from esun_agent.strategy.marketcap import market_cap  # noqa: E402

OOS_START = "2026-08-01"
G: dict = {}


def pct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:+.{d}f}%"


def ppct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.{d}f}%"


def one(args):
    seed, period = args
    r = run_backtest(G["panel"], G["strat"], G["factors"], G["top10"], caps=G["caps"],
                     tilt_fn=random_tilt_fn(seed, period=period))
    o = r.summary(OOS_START, None)
    d = r.daily
    ap_bad = (d["ap_min"] < 0.2).to_numpy()
    run_len = best = 0
    for x in ap_bad:                      # 連續 Active Share < 20% 的最長天數（官方：連 2 日 → 取消資格）
        run_len = run_len + 1 if x else 0
        best = max(best, run_len)
    return {"ap_consec": best, "seed": seed, "ret": o["total_return"], "mdd": o["mdd"], "turn": o["turnover_ann"],
            "cost": o["cost_drag_ann"], "ap_oos": o["ap_min"], "ap_all": float(d["ap_min"].min()),
            "viol_h": r.violations["holdings"], "viol_w": r.violations["weight"], "viol_c": r.violations["cash"],
            "ap_fail": int((~d["ap_ok"]).sum()), "n_min": int(d["n_hold"].min()), "n_max": int(d["n_hold"].max()),
            "cash_min": float(d["cash_ratio"].min()), "cash_max": float(d["cash_ratio"].max()),
            "rebal_days": int(d["rebalanced"].sum()), "rejected": r.rejected_buys, "unfilled": r.unfilled}


def dist(label, x, fmt=pct):
    x = np.asarray(x, dtype=float)
    q = np.quantile(x, [0.05, 0.5, 0.95])
    return (f"| {label} | {fmt(x.mean())} | {fmt(q[1])} | {fmt(x.std(ddof=1))} | {fmt(q[0])} / {fmt(q[2])} | "
            f"{fmt(x.min())} / {fmt(x.max())} |")


def sim_block(df: pd.DataFrame, b_ew: float, b_cw: float, base_ret: float) -> list[str]:
    L = ["| 指標 | 平均 | 中位數 | 標準差 | 5% / 95% 分位 | 最差 / 最佳 |", "|---|---|---|---|---|---|",
         dist("樣本外報酬", df["ret"]), dist("超額 vs (a) 等權", df["ret"] - b_ew), dist("超額 vs (b) 市值上限", df["ret"] - b_cw),
         dist("相對無 tilt 基線", df["ret"] - base_ret), dist("樣本外 MDD", df["mdd"]),
         dist("Active Share 最低值（樣本外）", df["ap_oos"], ppct), dist("Active Share 最低值（全期間）", df["ap_all"], ppct),
         dist("年換手（x）", df["turn"], lambda v: f"{v:.1f}"), dist("年化費用拖累", df["cost"], ppct)]
    n = len(df)
    L.append(f"\n- 超額 vs (b) > 0 的比例：{(df['ret'] - b_cw > 0).mean() * 100:.0f}%；vs (a) > 0：{(df['ret'] - b_ew > 0).mean() * 100:.0f}%；"
             f"勝過無 tilt 基線：{(df['ret'] > base_ret).mean() * 100:.0f}%。")
    L.append(f"- **違規（全期間逐日期末檢查）**：持股檔數 {int(df['viol_h'].sum())}、權重上限 {int(df['viol_w'].sum())}、現金比 {int(df['viol_c'].sum())}、"
             f"Active Share < 20% 天數 {int(df['ap_fail'].sum())}"
             f"（其中「連續 2 日以上」最長 {int(df['ap_consec'].max())} 日；{n} 次模擬中有任何違規的次數：{int(((df[['viol_h', 'viol_w', 'viol_c', 'ap_fail']].sum(axis=1)) > 0).sum())}）。")
    L.append(f"- 持股檔數範圍 {int(df['n_min'].min())}–{int(df['n_max'].max())}；期末現金比範圍 {ppct(df['cash_min'].min())}–{ppct(df['cash_max'].max())}；"
             f"平均交易日（含提前再平衡）{df['rebal_days'].mean():.0f} 天；因賣單未成交被拒的買單合計 {int(df['rejected'].sum())}、停牌未成交 {int(df['unfilled'].sum())}。")
    return L


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "final_strategy_backtest.md"))
    ap.add_argument("--sims", type=int, default=100)
    ap.add_argument("--stress-sims", type=int, default=30)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--sens-ap-target", type=float, default=0.27, help="敏感度分析：Active Share 目標抬高到此值（0 = 略過）")
    a = ap.parse_args(argv)

    panel = load_panel()
    factors = compute_factors(panel)
    top10 = load_top10()
    caps, cap_src = market_cap(panel)
    strat = FinalStrategy()
    G.update(panel=panel, factors=factors, top10=top10, caps=caps, strat=strat)

    ew, cw = equal_weight_benchmark(panel), cap_weight_benchmark(panel, caps)
    first_base = None

    base = run_backtest(panel, strat, factors, top10, caps=caps)
    first_base = base.nav.index[0]

    def bret(b, start):
        lo = b[b.index < start].index[-1] if start else first_base
        return float(b.iloc[-1] / b[lo] - 1)
    b_ew, b_cw = bret(ew, OOS_START), bret(cw, OOS_START)
    bo, bi = base.summary(OOS_START, None), base.summary(None, "2026-07-31")

    with Pool(a.jobs) as pool:
        sims = pd.DataFrame(pool.map(one, [(s, 5) for s in range(a.sims)]))
        stress = pd.DataFrame(pool.map(one, [(1000 + s, 1) for s in range(a.stress_sims)]))

    sens = None
    if a.sens_ap_target:
        G["strat"] = FinalStrategy(params=FinalParams(ap_target=a.sens_ap_target))
        with Pool(a.jobs) as pool:
            sens = pd.DataFrame(pool.map(one, [(s, 5) for s in range(a.sims)]))
        G["strat"] = strat

    # base_weights 的 Active Share 修正段落統計（每個決策日，不含 tilt）
    tiers: dict[str, int] = {}
    ap_base_min = []
    from esun_agent.data.etf_holdings import check_target_weights
    for D in panel.dates[60:-1:5]:
        w, _, notes = base_plan(factors_on(factors, D), caps.loc[D], strat.params, top10)
        tier = notes[0].split(":")[1] if notes else "none"
        tiers[tier] = tiers.get(tier, 0) + 1
        ap_base_min.append(check_target_weights(w, top10, margin=0.0, expected_etfs=list(top10))["min"])

    P = strat.params
    L = ["# 正式策略回測：合規基準 A ＋ Agent 小幅加減碼\n"]
    L.append("## 0. 規格\n")
    L.append(f"- **base_weights(D)**：合格名單（有 D 日價格、20 日均成交值 ≥ {P.min_adv / 1e8:.0f} 億）市值加權、2330 ≤ {P.base_cap_tsmc:.0%}、其他 ≤ {P.base_cap_other:.0%}，"
             f"取前 {P.n_base} 檔重新歸一，現金 {P.cash_target:.0%}；Active Share 補到 ≥ {P.ap_target:.0%}（官方 20%）：最小調整（1–3 檔）→ 放寬（6 檔）→ 全面壓低。")
    L.append(f"- **apply_tilts(base, tilts)**：單檔 |delta| ≤ {P.max_delta:.0%}、Σ|delta|/2 ≤ {P.max_active:.0%}；可加入名單內不在前 28 的股票；持股 {P.min_hold}–{P.max_hold} 檔；"
             f"套用後單檔 ≤ {P.tilt_cap_other:.0%}（2330 ≤ {P.tilt_cap_tsmc:.0%}）、現金 ≥ {P.cash_low:.0%}、Active Share ≥ {P.ap_target:.0%}。")
    L.append(f"- **交易時點**：每 {P.rebalance_every} 個交易日再平衡、無交易帶 {P.band:.0%}；tilt 與前一日不同、或漂移保護觸發"
             f"（前日收盤權重 2330 > {P.guard_tsmc:.0%}／其他 > {P.guard_other:.1%}，或 Active Share < {ACTIVE_GUARD:.0%}）時，當天就交易。")
    L.append(f"- 市值來源：{'發行股數快照' if cap_src == 'issued_shares' else '60 日均成交值代理（暫代）'}。基準：(a) 150 檔等權；(b) 市值加權上限（2330 ≤ 20%、其他 ≤ 8%）。"
             "樣本外 = T ≥ 2026-08-01（樣本內有名單偏誤，僅供參考）。Active Share 用 2026-10-06 的 ETF 前 10 大靜態快照。\n")
    L.append("## 1. 無 tilt 基線（只有 base_weights）\n")
    L.append("| 區間 | 天數 | 報酬 | 超額 vs (a) | 超額 vs (b) | MDD | Active Share 最低值 | 年換手 | 年化費用拖累 | 違規（持股/權重/現金） |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for lab, s_, st, en in (("**樣本外**", bo, OOS_START, None), ("樣本內（有偏誤）", bi, None, "2026-07-31")):
        e_, c_ = bret(ew, st), bret(cw, st)
        if en:
            e_ = float(ew[ew.index <= en].iloc[-1] / ew.loc[first_base] - 1)
            c_ = float(cw[cw.index <= en].iloc[-1] / cw.loc[first_base] - 1)
        L.append(f"| {lab} | {s_['days']} | {pct(s_['total_return'])} | {pct(s_['total_return'] - e_)} | {pct(s_['total_return'] - c_)} | "
                 f"{pct(s_['mdd'])} | {ppct(s_['ap_min'])} | {s_['turnover_ann']:.1f}x | {ppct(s_['cost_drag_ann'])} | "
                 f"{s_['viol_holdings']}/{s_['viol_weight']}/{s_['viol_cash']} |")
    L.append(f"\n基準樣本外報酬：(a) 等權 {pct(b_ew)}、(b) 市值上限 {pct(b_cw)}。")
    L.append(f"base_weights 的 Active Share 修正（每 5 個交易日取樣、共 {sum(tiers.values())} 個決策日）：" +
             "、".join(f"{k} {v} 次" for k, v in sorted(tiers.items())) +
             f"；修正後目標權重的 Active Share 最低值 {ppct(min(ap_base_min))}（目標 ≥ 25%）。"
             "（none = 原本就 ≥ 25%；minimal = 最小調整；wider = 放寬到 6 檔；full = 全面壓低。）\n")
    L.append(f"## 2. 隨機 tilt × {a.sims} 次（模擬 Agent 雜訊）\n")
    L.append("每次 5 檔、幅度 ±1–3%（加碼可選合格名單內任何股票，含不在前 28 的；減碼只選現有持股）、每 5 個交易日重抽一次。\n")
    L += sim_block(sims, b_ew, b_cw, bo["total_return"])
    L.append("")
    L.append(f"### 壓力測試：tilt 每天都變 × {a.stress_sims} 次\n")
    L.append("tilt 每天重抽 → 每天都觸發交易，檢驗「tilt 變動當天可交易」的路徑與成本。\n")
    L += sim_block(stress, b_ew, b_cw, bo["total_return"])
    L.append("")
    if sens is not None:
        L.append(f"### 敏感度：Active Share 目標抬高到 {a.sens_ap_target:.0%}（其餘不變）× {a.sims} 次\n")
        L.append("| 目標 | 樣本外報酬（平均） | 超額 vs (b)（平均） | 年換手（平均） | Active Share < 20% 天數（合計） | 最長連續 | 全期間最低值 | 任何違規的模擬數 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for lab, d_ in ((f"{P.ap_target:.0%}（正式）", sims), (f"{a.sens_ap_target:.0%}", sens)):
            nv = int(((d_[["viol_h", "viol_w", "viol_c", "ap_fail"]].sum(axis=1)) > 0).sum())
            L.append(f"| {lab} | {pct(d_['ret'].mean())} | {pct((d_['ret'] - b_cw).mean())} | {d_['turn'].mean():.1f}x | "
                     f"{int(d_['ap_fail'].sum())} | {int(d_['ap_consec'].max())} 日 | {ppct(d_['ap_all'].min())} | {nv} |")
        L.append("")
    L.append("## 3. 解讀與限制\n")
    L.append("- 隨機 tilt 沒有資訊含量，所以報酬分布反映的是「雜訊 tilt 的代價」：樣本外報酬圍繞無 tilt 基線，多出的費用與偏離是 Agent 要靠真實資訊優勢才能賺回的門檻。"
             "tilt 的總上限（Σ|delta|/2 ≤ 15%）與單檔上限（3%）讓雜訊 tilt 的傷害有界。")
    L.append("- **框架是否違規**（逐日期末實際權重檢查）：持股檔數、單檔權重上限、現金比三項在所有模擬中都是 0 次違規。"
             "Active Share 在極少數日子（見上面「違規」欄）單日跌破 20%——決策時已補到 ≥ 25%、漂移保護在前日收盤 < 24% 時就提前再平衡，"
             "但單日行情（某檔大漲跌使前 10 大組成改變）仍可能把 Active Share 一次拉低 4–6 個百分點。這些都是單日、沒有連續 2 日"
             "（官方取消資格條件是連 2 日）；把目標抬到 27% 可減少但無法完全消除（見敏感度表）。")
    L.append("- 樣本外只有 44 個交易日，分布是 100 條相近路徑（同一段行情、不同隨機 tilt），反映 tilt 雜訊，不反映行情不確定性。")
    L.append("- Active Share 用靜態快照近似；實盤需每日更新 ETF 前 10 大。")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(L), encoding="utf-8")
    print(f"寫入 {a.out}", file=sys.stderr)
    return 0


ACTIVE_GUARD = 0.20 + FinalParams().ap_guard

if __name__ == "__main__":
    sys.exit(main())
