#!/usr/bin/env python3
"""歷年「初賽同期」研究：每年 10/26 前最後一個交易日收盤 → 11/27（含）前最後一個交易日收盤。

只抓少量資料（每年：TAIEX 兩個月日線 + 起訖兩天的全市場收盤 + 選配的期間投信買賣超），
存到 data/seasonal/（不碰 data/market，避免影響回測），輸出 docs/seasonal_1026_1127.md。

用法：
  python scripts/seasonal_study.py                 # 2021–2025，只看價格
  python scripts/seasonal_study.py --inst          # 另抓期間每日投信買賣超（約多 10 分鐘）
  python scripts/seasonal_study.py --years 2022 2023

注意：150 檔名單是 2026-07 才定的 → 對過去年份有倖存者偏誤，只當描述性參考。
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from esun_agent.data.market import MarketFetcher, to_number  # noqa: E402
from esun_agent.universe import load_universe  # noqa: E402

OUT = ROOT / "data" / "seasonal"
FMTQIK = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
MI_INDEX = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"


def roc_to_date(s: str) -> date:
    y, m, d = str(s).strip().split("/")
    y = int(y)
    return date(y + 1911 if y < 1911 else y, int(m), int(d))


def taiex_daily(f: MarketFetcher, year: int) -> pd.Series:
    rows = {}
    for month in (10, 11):
        p = f.get_json(FMTQIK, {"date": f"{year}{month:02d}01", "response": "json"})
        fields = p["fields"]
        i_d = fields.index("日期")
        i_x = next(i for i, n in enumerate(fields) if "加權" in n and "指數" in n)
        for r in p["data"]:
            rows[roc_to_date(r[i_d])] = to_number(r[i_x])
    return pd.Series(rows).sort_index()


def twse_close_all(f: MarketFetcher, d: date, uni: dict) -> dict:
    p = f.get_json(MI_INDEX, {"date": d.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"})
    for t in p.get("tables", []):
        fl = [str(x) for x in t.get("fields", [])]
        if "證券代號" in fl and "收盤價" in fl:
            ic, ip = fl.index("證券代號"), fl.index("收盤價")
            out = {}
            for r in t.get("data", []):
                c = str(r[ic]).strip()
                if c in uni and uni[c]["market"] == "TWSE":
                    v = to_number(r[ip])
                    if v == v and v > 0:  # 排除 NaN / 0（無成交）
                        out[c] = v
            return out
    raise KeyError(f"MI_INDEX {d} 找不到個股收盤表")


def tpex_close_all(f: MarketFetcher, d: date) -> dict:
    df = f.tpex_day(d)
    return dict(zip(df.ticker, df.close))


def window(taiex: pd.Series, year: int):
    before = taiex[taiex.index < date(year, 10, 26)]
    inside = taiex[(taiex.index >= date(year, 10, 26)) & (taiex.index <= date(year, 11, 27))]
    return before.index[-1], inside


def study_year(f: MarketFetcher, year: int, uni: dict, with_inst: bool) -> dict:
    cache = OUT / f"{year}.json"
    if cache.exists() and (not with_inst or "trust_net" in json.loads(cache.read_text())):
        return json.loads(cache.read_text())
    tx = taiex_daily(f, year)
    base_d, inside = window(tx, year)
    end_d = inside.index[-1]
    path = pd.concat([pd.Series({base_d: tx[base_d]}), inside])
    rets = path.pct_change().dropna()
    nav = path / path.iloc[0]
    start_px = {**twse_close_all(f, base_d, uni), **tpex_close_all(f, base_d)}
    end_px = {**twse_close_all(f, end_d, uni), **tpex_close_all(f, end_d)}
    res = {
        "year": year, "base": base_d.isoformat(), "end": end_d.isoformat(), "days": len(inside),
        "taiex_ret": nav.iloc[-1] - 1, "taiex_mdd": float((nav / nav.cummax() - 1).min()),
        "worst_day": [rets.idxmin().isoformat(), float(rets.min())],
        "best_day": [rets.idxmax().isoformat(), float(rets.max())],
        "taiex_path": {k.isoformat(): v for k, v in path.items()},
        "start_px": start_px, "end_px": end_px,
    }
    if with_inst:
        trust = {}
        for d in inside.index:
            for df in (f.twse_inst(d), f.tpex_inst(d)):
                for t, v in zip(df.ticker, df.trust_net):
                    trust[t] = trust.get(t, 0.0) + float(v)
        res["trust_net"] = trust
    OUT.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return res


def report(results: list[dict], uni: dict) -> str:
    name = lambda t: uni[t]["name"]
    L = ["# 歷年初賽同期（10/26 前收盤 → 11/27 前收盤）", "",
         "150 檔名單為 2026-07 選出，過去年份有倖存者偏誤；個股報酬為未還原收盤價（期間若有分割/減資會失真，|報酬|>60% 已標記）。", "",
         "| 年 | 期間 | 交易日 | 加權指數 | 期間最大回檔 | 最差單日 | 最佳單日 | 150 檔等權 | 上漲家數比 | 投信買超前10 vs 賣超前10 |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    detail = []
    for r in results:
        s, e = r["start_px"], r["end_px"]
        rets = pd.Series({t: e[t] / s[t] - 1 for t in s if t in e})
        odd = rets[rets.abs() > 0.6]
        clean = rets.drop(odd.index)
        inst = "—"
        if "trust_net" in r:
            tn = pd.Series(r["trust_net"]).reindex(clean.index).dropna()
            top, bot = tn.nlargest(10).index, tn.nsmallest(10).index
            inst = f"{clean[top].mean():+.1%} vs {clean[bot].mean():+.1%}"
        L.append(f"| {r['year']} | {r['base']}→{r['end']} | {r['days']} | {r['taiex_ret']:+.1%} | {r['taiex_mdd']:.1%} | "
                 f"{r['worst_day'][0][5:]} {r['worst_day'][1]:+.1%} | {r['best_day'][0][5:]} {r['best_day'][1]:+.1%} | "
                 f"{clean.mean():+.1%} | {(clean > 0).mean():.0%} | {inst} |")
        srt = clean.sort_values()
        fmt = lambda x: "、".join(f"{name(t)} {v:+.0%}" for t, v in x.items())
        detail += [f"## {r['year']}", f"- 最佳 10：{fmt(srt[-10:][::-1])}", f"- 最差 10：{fmt(srt[:10])}",
                   f"- 有資料檔數 {len(rets)}" + (f"；疑似分割/減資已排除：{', '.join(name(t) for t in odd.index)}" if len(odd) else ""), ""]
    return "\n".join(L + [""] + detail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, default=[2021, 2022, 2023, 2024, 2025])
    ap.add_argument("--inst", action="store_true", help="另抓期間每日投信買賣超")
    ap.add_argument("--sleep", type=float, default=3.0)
    a = ap.parse_args()
    uni = load_universe()
    f = MarketFetcher(sleep=a.sleep)
    results = []
    for y in a.years:
        print(f"{y} …", flush=True)
        results.append(study_year(f, y, uni, a.inst))
    md = report(results, uni)
    (ROOT / "docs" / "seasonal_1026_1127.md").write_text(md, encoding="utf-8")
    print(md.split("\n\n")[2])
    print("\n完整結果：docs/seasonal_1026_1127.md")


if __name__ == "__main__":
    main()
