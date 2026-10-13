#!/usr/bin/env python
"""集中主題＋事件驅動策略的代理回測（候選池分數前 11 名代替 LLM）→ docs/conviction_backtest.md／.json。

  python scripts/run_conviction_backtest.py
24 日窗口：從 2025-04-01 起每 2 個交易日一個起點，每個窗口從 10 億現金重新建倉（含建倉成本），
報告相對「市值加權上限基準 (b)」（2330 ≤ 20%、其他 ≤ 8%，每日再平衡、不計成本）的超額分布。
另跑：遲滯代理（已持有且名次 ≤ 30 者續抱）敏感度、舊版合規基準 A（降級保底）對照、全期間連續回測。
樣本外 = 窗口起點 ≥ 2026-08-01；之前的結果有名單偏誤（見報告第 4 節）。
"""
import argparse
import json
import sys
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esun_agent import conviction_backtest as cb  # noqa: E402
from esun_agent.backtest import cap_weight_benchmark, perf, run_backtest  # noqa: E402
from esun_agent.config import ACTIVE_SHARE_MIN, ROOT  # noqa: E402
from esun_agent.data.etf_holdings import load_top10  # noqa: E402
from esun_agent.data.prices import load_panel  # noqa: E402
from esun_agent.strategy.candidates import CandidateParams  # noqa: E402
from esun_agent.strategy.conviction import ConvictionParams, load_event_calendar  # noqa: E402
from esun_agent.strategy.factors import compute_factors  # noqa: E402
from esun_agent.strategy.final import FinalStrategy  # noqa: E402
from esun_agent.strategy.marketcap import market_cap  # noqa: E402

FIRST = "2025-04-01"
OOS_START = "2026-08-01"
LENGTH, STEP = 24, 2
G: dict = {}


def pct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:+.{d}f}%"


def ppct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.{d}f}%"


def conv_window(args):
    s, variant = args
    dates = G["panel"].dates
    proxy = {"hysteresis": cb.ProxyParams(keep_rank=30), "tsmc": cb.ProxyParams(fixed=(("2330", "high"),)),
             "strict": cb.ProxyParams()}[variant]
    r = cb.run_conviction_backtest(G["panel"], G["factors"], G["caps"], G["top10"], dates[s], dates[s + LENGTH - 1],
                                   proxy=proxy, events=G["events"])
    out = cb.window_stats(r, G["cw"], dates, s, LENGTH)
    out["drift_days"] = int(r.daily["v_weight"].sum())
    out["buy_over_cap_days"] = int(r.daily["v_weight_buy"].sum())
    out["v_holdings"] = int(r.daily["v_holdings"].sum())
    out["v_cash"] = int(r.daily["v_cash"].sum())
    return out


def base_window(s):
    dates = G["panel"].dates
    a, b = dates[s], dates[s + LENGTH - 1]
    r = run_backtest(G["panel"], G["final"], G["factors"], G["top10"], start=a, end=b, caps=G["caps"])
    p = perf(r.nav)
    cw = G["cw"][(G["cw"].index >= dates[s - 1]) & (G["cw"].index <= b)]
    cw_ret = float(cw.iloc[-1] / cw.iloc[0] - 1)
    d = r.daily
    return {"start": a, "end": b, "ret": p["total_return"], "ex_cw": p["total_return"] - cw_ret, "mdd": p["mdd"],
            "turnover": float(d["turnover"].sum()), "cost": float((d["fee"].sum() + d["tax"].sum()) / r.nav.iloc[0]),
            "viol": sum(r.violations.values()), "ap_fail_days": int((d["ap_min"] < ACTIVE_SHARE_MIN).sum())}


def dist_row(label, s, fmt=pct):
    x = np.asarray(s, dtype=float)
    q = np.quantile(x, [0.05, 0.5, 0.95])
    return f"| {label} | {fmt(x.mean())} | {fmt(q[1])} | {fmt(q[0])} / {fmt(q[2])} | {fmt(x.min())} / {fmt(x.max())} |"


def prob_row(label, d):
    return (f"| {label} | {d['n']} | {ppct(d['p_gt_0'], 0)} | {ppct(d['p_gt_5'], 0)} | {ppct(d['p_gt_10'], 0)} | "
            f"{ppct(d['p_lt_m5'], 0)} | {ppct(d['p_lt_m10'], 0)} | {pct(d['mean'])} | {pct(d['median'])} |")


def block(df: pd.DataFrame) -> dict:
    out = {"excess": cb.excess_distribution(df["ex_cw"]), "n": int(len(df))}
    for k in ("ret", "mdd", "turnover", "cost"):
        x = df[k].to_numpy(dtype=float)
        out[k] = {"mean": float(x.mean()), "median": float(np.median(x)), "p5": float(np.quantile(x, 0.05)),
                  "p95": float(np.quantile(x, 0.95)), "min": float(x.min()), "max": float(x.max())}
    for k in ("viol", "ap_fail_days", "drift_days", "buy_over_cap_days", "v_holdings", "v_cash", "build_errors",
              "stop_losses", "swaps"):
        if k in df:
            out[f"sum_{k}"] = int(df[k].sum())
    if "ap_min" in df:
        out["ap_min"] = float(df["ap_min"].min())
        out["ap_max_consecutive"] = int(df["ap_max_consecutive"].max())
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "conviction_backtest.md"))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--no-base", action="store_true", help="略過舊版合規基準 A 的窗口對照（較慢）")
    a = ap.parse_args(argv)

    panel = load_panel()
    factors = compute_factors(panel)
    caps, cap_src = market_cap(panel)
    top10 = load_top10()
    cw = cap_weight_benchmark(panel, caps)
    events = load_event_calendar()
    G.update(panel=panel, factors=factors, caps=caps, top10=top10, cw=cw, events=events, final=FinalStrategy())
    starts = cb.window_starts(panel.dates, FIRST, LENGTH, STEP)

    with Pool(a.jobs) as pool:
        main_df = pd.DataFrame(pool.map(conv_window, [(s, "strict") for s in starts]))
        hyst_df = pd.DataFrame(pool.map(conv_window, [(s, "hysteresis") for s in starts]))
        tsmc_df = pd.DataFrame(pool.map(conv_window, [(s, "tsmc") for s in starts]))
        base_df = None if a.no_base else pd.DataFrame(pool.map(base_window, starts))

    full = cb.run_conviction_backtest(panel, factors, caps, top10, FIRST, None, events=events)
    full_h = cb.run_conviction_backtest(panel, factors, caps, top10, FIRST, None, events=events,
                                        proxy=cb.ProxyParams(keep_rank=30))
    full_free = cb.run_conviction_backtest(panel, factors, caps, top10, FIRST, None, events=events, cost_free=True)

    def bench_ret(start, end=None):
        b = cw[cw.index >= full.nav[full.nav.index < start].index[-1]] if start > full.nav.index[0] else cw[cw.index >= full.nav.index[0]]
        if end:
            b = b[b.index <= end]
        return float(b.iloc[-1] / b.iloc[0] - 1), perf(b)["mdd"]

    periods = [("全期間", FIRST, None), ("樣本內（有偏誤）", FIRST, "2026-07-31"), ("**樣本外**", OOS_START, None)]
    oos = main_df[main_df["start"] >= OOS_START]
    oos_h = hyst_df[hyst_df["start"] >= OOS_START]
    ins = main_df[main_df["start"] < OOS_START]
    ex_all, ex_oos, ex_is = (cb.excess_distribution(x["ex_cw"]) for x in (main_df, oos, ins))
    P, C = ConvictionParams(), CandidateParams()

    L = ["# 集中主題＋事件驅動：代理回測（24 日窗口）\n"]
    L.append("> LLM 無法回測，本報告以「候選池分數前 11 名（前 5 名高信心、其餘中信心）」代替 LLM 選股，"
             "其餘規則（權重換算、湊檔數部位、現金、Active Share、換股紀律、事件前一日不新增、無交易帶、委託公式）與實盤同一套程式。"
             "結果反映的是**候選池分數 + 規則**的表現，不是 LLM 的表現。\n")
    L.append("## 0. 規格\n")
    L.append(f"- 高信心部位 {P.n_conv_min}–{P.n_conv_max} 檔：高 {P.w_high:.0%}、中 {P.w_mid:.0%}（2330：高 {P.w_tsmc_high:.0%}、中 {P.w_tsmc_mid:.0%}）；"
             f"單檔上限 {P.cap_other:.0%}（2330 {P.cap_tsmc:.0%}）。湊檔數部位：市值前 {P.filler_pool} 大中不在名單者，各 {P.filler_min:.0%}–{P.filler_max:.0%}，"
             f"總持股 {P.n_total_min}–{P.n_total_max} 檔；現金目標 {P.cash_target:.0%}（{P.cash_low:.0%}–{P.cash_high:.0%}）。")
    L.append(f"- 候選池（每日 {C.n_candidates} 檔，20 日均成交值 ≥ {C.min_adv / 1e8:.0f} 億）：投信 5 日 {C.w_trust5}、10 日 {C.w_trust10}（成交值標準化）、"
             f"20 日相對強度 {C.w_rs20}、成交值放大 {C.w_valchg}，減去 {C.overlap_penalty} × 主動型 ETF 前 15 大重疊比例。")
    L.append(f"- 紀律：持有 ≥ {P.min_hold_days} 日才可賣出／降級（停損除外）；每日最多新增 {P.max_swaps} 檔、主動移出 {P.max_swaps} 檔；"
             f"成本下跌 > {P.stop_loss:.0%} 出場（代理沒有新正面事件可引用，一律出場）；FOMC／NVIDIA 財報／美國大選前一交易日凍結（{len(events)} 個事件）；"
             f"無交易帶 {P.band:.1%}；前日收盤權重 > {P.guard_other:.1%}（2330 > {P.guard_tsmc:.0%}）當日減回。")
    L.append(f"- Active Share：前 15 大 vs 各主動型 ETF（快照只有前 10 大）官方 ≥ {ACTIVE_SHARE_MIN:.0%}，內部目標 ≥ {P.ap_target:.0%}；"
             f"前日 < {P.ap_trigger:.0%} 當日強制再平衡、前日 < 15% 補到 {P.ap_recovery_target:.0%}。")
    L.append(f"- 窗口：{LENGTH} 個交易日、每 {STEP} 個交易日一個起點（共 {len(main_df)} 個，起點 {main_df['start'].iloc[0]} ～ {main_df['start'].iloc[-1]}），"
             "每個窗口從 10 億現金重新建倉（含建倉費用）。基準 (b)：市值加權上限（2330 ≤ 20%、其他 ≤ 8%），每日再平衡、不計成本。"
             f"市值來源：{'發行股數快照' if cap_src == 'issued_shares' else '成交值代理'}。\n")

    L.append("## 1. 24 日超額報酬分布（相對基準 b）\n")
    L.append("| 樣本 | 窗口數 | P(超額 > 0) | **P(> +5%)** | **P(> +10%)** | P(< −5%) | **P(< −10%)** | 平均 | 中位數 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    L.append(prob_row("全部（代理，嚴格前 11 名）", ex_all))
    L.append(prob_row("　樣本內（起點 < 2026-08-01，有偏誤）", ex_is))
    L.append(prob_row("　**樣本外**（起點 ≥ 2026-08-01）", ex_oos))
    L.append(prob_row("遲滯代理（名次 ≤ 30 續抱）全部", cb.excess_distribution(hyst_df["ex_cw"])))
    L.append(prob_row("　遲滯代理 樣本外", cb.excess_distribution(oos_h["ex_cw"])))
    oos_t = tsmc_df[tsmc_df["start"] >= OOS_START]
    L.append(prob_row("2330 固定高信心（15%）＋前 10 名", cb.excess_distribution(tsmc_df["ex_cw"])))
    L.append(prob_row("　2330 固定 樣本外", cb.excess_distribution(oos_t["ex_cw"])))
    if base_df is not None:
        L.append(prob_row("舊版合規基準 A（降級保底）全部", cb.excess_distribution(base_df["ex_cw"])))
        L.append(prob_row("　合規基準 A 樣本外", cb.excess_distribution(base_df[base_df["start"] >= OOS_START]["ex_cw"])))
    L.append("")
    L.append("| 指標（嚴格代理，全部窗口） | 平均 | 中位數 | 5% / 95% | 最差 / 最佳 |")
    L.append("|---|---|---|---|---|")
    L.append(dist_row("24 日報酬", main_df["ret"]))
    L.append(dist_row("超額 vs (b)", main_df["ex_cw"]))
    L.append(dist_row("窗口內 MDD", main_df["mdd"]))
    L.append(dist_row("基準 (b) 窗口內 MDD", main_df["bench_mdd"]))
    L.append(dist_row("換手（單邊，占期初 NAV，含建倉）", main_df["turnover"], lambda v: f"{v:.2f}x"))
    L.append(dist_row("費用＋證交稅（占期初 NAV）", main_df["cost"], ppct))
    L.append(dist_row("停損次數", main_df["stop_losses"], lambda v: f"{v:.1f}"))
    L.append(dist_row("新增（換股）檔數", main_df["swaps"], lambda v: f"{v:.1f}"))
    L.append("")
    L.append("| 違規檢查（逐日期末實際權重） | 嚴格代理 | 遲滯代理 |" + (" 合規基準 A |" if base_df is not None else ""))
    L.append("|---|---|---|" + ("---|" if base_df is not None else ""))
    rows = [("持股檔數不在 20–30（天）", "v_holdings"), ("現金 < 0 或 ≥ 25%（天）", "v_cash"),
            ("Active Share < 15%（天）", "ap_fail_days"), ("組合建構失敗（天）", "build_errors")]
    for lab, k in rows:
        line = f"| {lab} | {int(main_df[k].sum())} | {int(hyst_df[k].sum())} |"
        if base_df is not None:
            line += f" {int(base_df[k].sum()) if k in base_df else '—'} |"
        L.append(line)
    L.append(f"| 單檔 > 10%／2330 > 25%（天，價格漂移；隔日即減回，官方給 5 日調整） | {int(main_df['drift_days'].sum())} | {int(hyst_df['drift_days'].sum())} |"
             + (" — |" if base_df is not None else ""))
    L.append(f"| 　其中「當天有買進該檔」（可能被認定為加碼交易造成） | {int(main_df['buy_over_cap_days'].sum())} | {int(hyst_df['buy_over_cap_days'].sum())} |"
             + (" — |" if base_df is not None else ""))
    L.append(f"\nActive Share 窗口內最低 {ppct(main_df['ap_min'].min())}（官方 15%）；連續低於 15% 最長 {int(main_df['ap_max_consecutive'].max())} 日。"
             "買進目標一律 ≤ 9%；超過 10% 的天數都來自收盤價漂移，前日收盤 > 9.5% 即於當日減回。\n")

    L.append("## 2. 全期間連續回測（2025-04-01 起，一次建倉）\n")
    L.append("| 區間 | 版本 | 報酬 | 基準 (b) | 超額 | MDD | 基準 MDD | 年化換手 | 費用（占期初 NAV） | 停損 | 換股（新增檔數） |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    full_rows = {}
    for lab, st, en in periods:
        br, bm = bench_ret(st, en)
        for vlab, r in (("嚴格代理", full), ("遲滯代理", full_h), ("嚴格代理・免費用", full_free)):
            s = r.summary(st, en)
            full_rows[f"{lab}|{vlab}"] = {**{k: s[k] for k in ("total_return", "mdd", "turnover_ann", "cost", "stop_losses", "swaps")},
                                         "bench": br, "bench_mdd": bm}
            L.append(f"| {lab} | {vlab} | {pct(s['total_return'])} | {pct(br)} | {pct(s['total_return'] - br)} | {pct(s['mdd'])} | {pct(bm)} | "
                     f"{s['turnover_ann']:.1f}x | {ppct(s['cost'])} | {s['stop_losses']} | {s['swaps']} |")
    L.append("")

    L.append("## 3. 解讀\n")
    gross_gap = full_free.summary(FIRST)["total_return"] - full.summary(FIRST)["total_return"]
    L.append(f"- **偏離確實拉大了**：24 日超額的 5%–95% 區間為 {pct(ex_all['p5'])} ～ {pct(ex_all['p95'])}，"
             + (f"合規基準 A 只有 {pct(cb.excess_distribution(base_df['ex_cw'])['p5'])} ～ {pct(cb.excess_distribution(base_df['ex_cw'])['p95'])}。" if base_df is not None else "")
             + "排名賽需要的是右尾（P(> +10%)），代價是左尾（P(< −10%)）同樣變厚。")
    L.append(f"- **換手與費用是最大的敵人**：嚴格代理每天都在追分數前 11 名，24 日窗口平均換手 {main_df['turnover'].mean():.1f}x、"
             f"費用平均 {ppct(main_df['cost'].mean())}；全期間免費用版本比含費用版本多 {pct(gross_gap)}。"
             "遲滯代理（已持有且名次 ≤ 30 就續抱）換手與費用明顯較低——實盤 LLM 應該比嚴格代理更有持續性（同一檔要有新證據才換），"
             "prompt 已要求「沒有新證據就續抱」。")
    L.append("- 候選池分數以短期籌碼與相對強度為主，信號衰減快；代理沒有基本面事件（營收、財報、法說），LLM 的價值在於用事件過濾掉只是短線熱門的名字。"
             "這部分無法回測，只能用初賽期間的實際結果檢驗。")
    L.append(f"- 違規：持股檔數、現金、Active Share 在所有窗口都是 {int(main_df['v_holdings'].sum())}／{int(main_df['v_cash'].sum())}／{int(main_df['ap_fail_days'].sum())} 天；"
             f"單檔超過 10% 的 {int(main_df['drift_days'].sum())} 天中，{int(main_df['buy_over_cap_days'].sum())} 天是當天買進該檔（目標 ≤ 9%）、收盤卻超過 10%（例如買進當天漲停而淨值下跌），"
             "主辦方可能認定為加碼造成而記警告——這是 9% 上限的殘餘風險；其餘都是持有中的價格漂移，前日收盤 > 9.5% 即於當日減回，不會超過官方 5 日調整期。")
    L.append("")
    L.append("## 4. 樣本外與名單偏誤\n")
    L.append(f"- 150 檔名單是 2026 年 7 月底依市值選出：2025 年到 2026 年 7 月間大漲而擠進名單的股票被「事後」納入，"
             "動能／籌碼型的候選池分數特別容易買到它們，**樣本內（2026-08-01 以前）的報酬與超額都被高估**，只能用來比較規則的相對優劣。"
             f"基準 (b) 也用同一份名單，所以相對基準的超額偏誤較小，但仍偏高（集中持有贏家的效果比市值加權大）。")
    L.append(f"- Active Share 的 ETF 持股是 2026-10-06 的前 10 大快照（MoneyDJ 只揭露 10 檔），套用到整段回測；候選池的「主動型 ETF 重疊扣分」也用這份快照，"
             "對 2025 年的窗口有前視。")
    L.append(f"- **樣本外以窗口起點 ≥ {OOS_START} 為準**：只有 {ex_oos['n']} 個窗口，且彼此重疊（每 2 日一個起點、長 24 日），有效獨立樣本約 1–2 個，"
             "機率估計的誤差很大，請把它當成方向性參考。")
    L.append("- 未計股利（官方除息現金期末才加回，兩邊都不含）；成交一律以均價、無市場衝擊（單檔 9% ≈ 9000 萬，候選池要求 20 日均成交值 ≥ 3 億）。")

    def records(df):
        return [{k: cb.nan_to_none(v) if not isinstance(v, (np.integer,)) else int(v) for k, v in r.items()}
                for r in df.to_dict("records")]
    summary = {"first": FIRST, "oos_start": OOS_START, "length": LENGTH, "step": STEP, "cap_source": cap_src,
               "universe_dates": [panel.dates[0], panel.dates[-1]], "n_events": len(events),
               "strict": block(main_df), "strict_oos": block(oos), "strict_is": block(ins),
               "hysteresis": block(hyst_df), "hysteresis_oos": block(oos_h),
               "tsmc_fixed": block(tsmc_df), "tsmc_fixed_oos": block(oos_t),
               "base_a": block(base_df) if base_df is not None else None,
               "base_a_oos": block(base_df[base_df["start"] >= OOS_START]) if base_df is not None else None,
               "full_period": full_rows, "windows": records(main_df),
               "params": {k: getattr(P, k) for k in P.__dataclass_fields__},
               "candidate_params": {k: getattr(C, k) for k in C.__dataclass_fields__}}
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"寫入 {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
