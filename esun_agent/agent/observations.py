"""把行情、三大法人、台指期夜盤、美股／ADR、新聞與公開資訊事件轉成 D-Plan 的 sources 與 observations。

原則（撰寫指南 ②）：observations 只寫「可核對的事實」，values 只放數字，不放判斷。
authority 依 config/strategy_declaration.json 的 tilt.allowed_bases：
  財報／月營收／重大訊息 → mops；法說會 → mops 或 media；投信連續買賣超 → twse（上市）／tpex（上櫃）。
其他：台指期夜盤 taifex、美股／ADR vendor、新聞 media、個股價量 twse／tpex（上市／上櫃各自一個 source，不可混用）。

這個模組不抓網路；資料來源（台指期夜盤、美股、新聞、MOPS 事件）由 collector 寫成下面「輸入格式」的 JSON 後交進來。
輸入格式（皆為 dict，時間 ISO +08:00）：
  taifex: {"url", "content_as_of", "session_date", "close", "chg_pct", "volume", "name"?}
  us:     {"url", "content_as_of", "name"?, "quotes": {"SOX": {"close", "chg_pct"}, "TSM": {...}, ...}}
  events: [{"basis": 五種依據 id 或 null(一般新聞), "ticker": "2330"|null, "authority", "url", "name"?,
            "published_at"?, "content_as_of", "archive_url"?, "statement"(只寫事實), "values"(只放數字)}]
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..data.market import source_info
from ..strategy.declaration import SOURCE_AUTHORITIES, load_declaration
from ..universe import authority_for, load_universe

TZ = timezone(timedelta(hours=8))
BASIS_LABELS = {"financial_report": "財報", "investor_conference": "法說會", "monthly_revenue": "月營收",
                "trust_flow": "投信連續買賣超", "material_news": "重大訊息"}


def now_taipei() -> datetime:
    return datetime.now(TZ).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(TZ).replace(microsecond=0).isoformat()


def clean_values(values: dict) -> dict:
    """values 只放數字（int／float，不含 bool、NaN、inf）。"""
    out = {}
    for k, v in values.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"observation values 只能放有限數字：{k}={v!r}")
        out[str(k)] = round(float(v), 6) if isinstance(v, float) else v
    return out


@dataclass
class Evidence:
    sources: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    meta: dict[str, dict] = field(default_factory=dict)       # obs_id -> {ticker, basis, as_of, authority, carried}
    carried_ids: dict[str, list[str]] = field(default_factory=dict)   # ticker -> 由狀態檔重建的舊事件 obs_id

    def obs(self, obs_id: str) -> dict:
        return next(o for o in self.observations if o["obs_id"] == obs_id)

    def source_of(self, obs_id: str) -> list[dict]:
        refs = set(self.obs(obs_id)["source_ref"])
        return [s for s in self.sources if s["source_id"] in refs]

    def by_topic(self, topic: str) -> list[dict]:
        return [o for o in self.observations if o.get("topic") == topic]

    def payload(self, obs_id: str) -> dict:
        """可存進 tilt 狀態、之後重建成 observation 的完整內容（含來源）。"""
        o = self.obs(obs_id)
        return {"topic": o.get("topic"), "statement": o["statement"], "values": o["values"],
                "sources": self.source_of(obs_id), "as_of": self.meta[obs_id]["as_of"],
                "ticker": self.meta[obs_id].get("ticker"), "basis": self.meta[obs_id].get("basis")}


class EvidenceBuilder:
    def __init__(self, fetched_at: datetime | None = None, evidence: Evidence | None = None):
        self.ev = evidence or Evidence()
        self.fetched_at = iso(fetched_at) if fetched_at else None
        self._src: dict[tuple, str] = {(s["authority"], s["url"], s["content_as_of"]): s["source_id"]
                                       for s in self.ev.sources}

    def add_source(self, authority: str, url: str, content_as_of: str, name: str | None = None,
                   archive_url: str | None = None, published_at: str | None = None) -> str:
        if authority not in SOURCE_AUTHORITIES | {"other"}:
            raise ValueError(f"未知 authority: {authority}")
        key = (authority, url, content_as_of)
        if key in self._src:
            return self._src[key]
        sid = f"S{len(self.ev.sources) + 1}"
        src = {"source_id": sid, "authority": authority, "url": url, "content_as_of": content_as_of}
        if name:
            src["name"] = name[:300]
        if archive_url:
            src["archive_url"] = archive_url
        if published_at:
            src["published_at"] = published_at
        if self.fetched_at:
            src["fetched_at"] = self.fetched_at
        self.ev.sources.append(src)
        self._src[key] = sid
        return sid

    def add_obs(self, source_ids: list[str], topic: str, statement: str, values: dict, *, ticker: str | None = None,
                basis: str | None = None, as_of: str | None = None, carried: bool = False) -> str:
        if not 1 <= len(source_ids) <= 5:
            raise ValueError("source_ref 需 1–5 個")
        if not 5 <= len(statement) <= 500:
            raise ValueError(f"statement 長度需 5–500：{statement!r}")
        oid = f"O{len(self.ev.observations) + 1}"
        self.ev.observations.append({"obs_id": oid, "source_ref": list(source_ids), "topic": topic,
                                     "statement": statement, "values": clean_values(values)})
        auths = [s["authority"] for s in self.ev.sources if s["source_id"] in source_ids]
        as_of = as_of or max(s["content_as_of"] for s in self.ev.sources if s["source_id"] in source_ids)
        self.ev.meta[oid] = {"ticker": ticker, "basis": basis, "as_of": as_of, "authority": auths[0] if auths else None,
                             "authorities": auths, "carried": carried}
        return oid


# ----------------------------------------------------------------------------- 行情／法人

def _close_time(date: str) -> str:
    return f"{date}T13:30:00+08:00"


def market_sources(b: EvidenceBuilder, date: str) -> dict[str, str]:
    """上市（twse）與上櫃（tpex）各一個價量 source；回傳 {"twse": sid, "tpex": sid}。"""
    out = {}
    for auth in ("twse", "tpex"):
        info = source_info(date, auth)
        out[auth] = b.add_source(auth, info["url"], info["content_as_of"],
                                 name=f"{'TWSE' if auth == 'twse' else 'TPEx'} 日行情 {date}（個股價量）")
    return out


def institutional_sources(b: EvidenceBuilder, date: str) -> dict[str, str]:
    out = {}
    for auth in ("twse", "tpex"):
        info = source_info(date, auth, kind="institutional")
        out[auth] = b.add_source(auth, info["url"], f"{date}T17:30:00+08:00",
                                 name=f"{'TWSE T86' if auth == 'twse' else 'TPEx 三大法人'} 買賣超 {date}")
    return out


def market_overview(b: EvidenceBuilder, panel, date: str) -> str:
    """名單內 150 檔當日的整體表現（等權報酬、漲跌家數、成交值、2330）。"""
    srcs = market_sources(b, date)
    i = panel.dates.index(date)
    px = panel.adj_close.ffill(limit=10)
    ret = (px.iloc[i] / px.iloc[i - 1] - 1).where(panel.close.iloc[i].notna() & panel.close.iloc[i - 1].notna()).dropna()
    adv, dec = int((ret > 0.0005).sum()), int((ret < -0.0005).sum())
    val = float(panel.value.iloc[i].sum()) / 1e8
    r2330 = float(ret.get("2330", float("nan")))
    values = {"universe_n": int(len(ret)), "ew_ret_pct": float(ret.mean() * 100), "advancers": adv, "decliners": dec,
              "traded_value_twd_100m": val}
    st = (f"{date} 投資範圍 150 檔：等權平均 {ret.mean() * 100:+.2f}%，上漲 {adv} 檔、下跌 {dec} 檔，"
          f"成交值合計 {val:,.0f} 億元")
    if math.isfinite(r2330):
        values["tsmc_ret_pct"] = r2330 * 100
        st += f"；2330 {r2330 * 100:+.2f}%"
    return b.add_obs([srcs["twse"], srcs["tpex"]], "tw_market", st, values, as_of=_close_time(date))


def institutional_overview(b: EvidenceBuilder, panel, date: str) -> str:
    """名單內 150 檔當日外資、投信買賣超金額（以當日均價估算，單位：億元）。"""
    srcs = institutional_sources(b, date)
    i = panel.dates.index(date)
    avg = panel.avg_price.iloc[i].fillna(0.0)
    fo = float((panel.foreign_net.iloc[i] * avg).sum()) / 1e8
    tr = float((panel.trust_net.iloc[i] * avg).sum()) / 1e8
    values = {"foreign_net_twd_100m": fo, "trust_net_twd_100m": tr}
    st = f"{date} 投資範圍 150 檔：外資買賣超 {fo:+,.1f} 億元、投信買賣超 {tr:+,.1f} 億元（以當日均價估算）"
    return b.add_obs([srcs["twse"], srcs["tpex"]], "inst_flow", st, values, as_of=f"{date}T17:30:00+08:00")


def trust_streak(panel, ticker: str, i: int) -> tuple[int, int, float]:
    """截至第 i 個交易日，投信連續買超（+）／賣超（−）的天數、期間淨股數、占期間成交股數比。"""
    s = panel.trust_net[ticker].iloc[: i + 1]
    sign = 1 if s.iloc[-1] > 0 else -1 if s.iloc[-1] < 0 else 0
    if sign == 0:
        return 0, 0, 0.0
    n = 0
    for v in s.iloc[::-1]:
        if v * sign > 0:
            n += 1
        else:
            break
    net = float(s.iloc[-n:].sum())
    vol = float(panel.volume[ticker].iloc[i - n + 1: i + 1].sum())
    return sign * n, int(net), (net / vol if vol else 0.0)


def trust_flow_observations(b: EvidenceBuilder, panel, date: str, tickers: list[str], *, min_streak: int = 3,
                            limit: int = 15) -> list[str]:
    """投信連續買／賣超 ≥ min_streak 日的個股（優先 tickers 內，依 |占比| 排序，最多 limit 檔）。"""
    srcs = institutional_sources(b, date)
    i = panel.dates.index(date)
    rows = []
    for t in tickers:
        if t not in panel.trust_net.columns or pd.isna(panel.close[t].iloc[i]):
            continue
        n, net, ratio = trust_streak(panel, t, i)
        if abs(n) >= min_streak:
            rows.append((abs(ratio), t, n, net, ratio))
    out = []
    for _, t, n, net, ratio in sorted(rows, reverse=True)[:limit]:
        verb = "買超" if n > 0 else "賣超"
        st = f"{t} 投信連續 {abs(n)} 日{verb}，期間淨{verb[:1]}{abs(net) / 1000:,.0f} 張，占期間成交量 {abs(ratio) * 100:.2f}%"
        out.append(b.add_obs([srcs[authority_for(t)]], "trust_flow", st,
                             {"streak_days": n, "net_shares": net, "net_ratio_pct": ratio * 100},
                             ticker=t, basis="trust_flow", as_of=f"{date}T17:30:00+08:00"))
    return out


# ----------------------------------------------------------------------------- 夜盤／美股／事件

def taifex_observation(b: EvidenceBuilder, d: dict) -> str:
    sid = b.add_source("taifex", d["url"], d["content_as_of"], name=d.get("name") or "TAIFEX 台指期夜盤")
    chg = d["chg_pct"]
    st = f"台指期夜盤（{d.get('session_date', '')}）收 {d['close']:,.0f}，{chg:+.2f}%"
    values = {"close": d["close"], "chg_pct": chg}
    if "volume" in d:
        values["volume"] = d["volume"]
    return b.add_obs([sid], "taifex_night", st, values)


def us_observation(b: EvidenceBuilder, d: dict) -> str:
    sid = b.add_source("vendor", d["url"], d["content_as_of"], name=d.get("name") or "美股／ADR 收盤行情")
    parts, values = [], {}
    for name, q in d["quotes"].items():
        parts.append(f"{name} {q['chg_pct']:+.2f}%")
        values[f"{name}_chg_pct"] = q["chg_pct"]
        if "close" in q:
            values[f"{name}_close"] = q["close"]
    return b.add_obs([sid], "us_overnight", "美股／ADR 收盤：" + "、".join(parts), values)


def event_observation(b: EvidenceBuilder, e: dict, declaration: dict | None = None) -> str:
    """一則 MOPS／新聞事件 → observation。basis 不為 None 時檢查 authority 是否為宣告檔允許的來源。"""
    decl = declaration or load_declaration()
    basis, ticker = e.get("basis"), e.get("ticker")
    if basis is not None:
        allowed = next((x for x in decl["tilt"]["allowed_bases"] if x["id"] == basis), None)
        if allowed is None:
            raise ValueError(f"未知的 tilt 依據 {basis}")
        if e["authority"] not in allowed["authority"]:
            raise ValueError(f"{BASIS_LABELS[basis]} 的來源 authority 應為 {allowed['authority']}，收到 {e['authority']}")
    if ticker is not None and ticker not in load_universe():
        raise ValueError(f"{ticker} 不在 150 檔名單")
    sid = b.add_source(e["authority"], e["url"], e["content_as_of"], name=e.get("name"),
                       archive_url=e.get("archive_url"), published_at=e.get("published_at"))
    topic = basis or "news"
    return b.add_obs([sid], topic, e["statement"], e.get("values", {}), ticker=ticker, basis=basis,
                     as_of=e["content_as_of"])


def carried_observation(b: EvidenceBuilder, p: dict) -> str:
    """把狀態檔裡保存的舊事件（含來源）重建成今天的 observation（as_of 不變，標記 carried）。"""
    sids = [b.add_source(s["authority"], s["url"], s["content_as_of"], name=s.get("name"),
                         archive_url=s.get("archive_url"), published_at=s.get("published_at"))
            for s in p["sources"]]
    return b.add_obs(sids, p["topic"] or "news", p["statement"], p["values"], ticker=p.get("ticker"),
                     basis=p.get("basis"), as_of=p["as_of"], carried=True)


def build_evidence(panel, date: str, *, inputs: dict | None = None, focus_tickers: list[str] | None = None,
                   carried: dict[str, list[dict]] | None = None, fetched_at: datetime | None = None,
                   declaration: dict | None = None) -> Evidence:
    """date = 決策日 D（資料描述的交易日）。回傳 Evidence（sources、observations、meta）。
    順序：台股整體 → 三大法人整體 → 台指期夜盤 → 美股 → 個股投信連續買賣超 → 事件／新聞 → 沿用中的舊事件。"""
    inputs = inputs or {}
    b = EvidenceBuilder(fetched_at)
    market_overview(b, panel, date)
    institutional_overview(b, panel, date)
    if inputs.get("taifex"):
        taifex_observation(b, inputs["taifex"])
    if inputs.get("us"):
        us_observation(b, inputs["us"])
    if focus_tickers:
        trust_flow_observations(b, panel, date, focus_tickers)
    for e in inputs.get("events", []):
        event_observation(b, e, declaration)
    for ticker, payloads in (carried or {}).items():
        b.ev.carried_ids[ticker] = [carried_observation(b, p) for p in payloads]
    return b.ev


def prune(ev: Evidence, used_obs: set[str]) -> tuple[list[dict], list[dict], dict[str, str]]:
    """只保留被引用的 observations 與它們的 sources，重新連號（S1…、O1…）。回傳 (sources, observations, {舊 obs_id: 新 obs_id})。"""
    keep = [o for o in ev.observations if o["obs_id"] in used_obs]
    used_src = {s for o in keep for s in o["source_ref"]}
    smap, sources = {}, []
    for s in ev.sources:
        if s["source_id"] in used_src:
            smap[s["source_id"]] = f"S{len(sources) + 1}"
            sources.append({**s, "source_id": smap[s["source_id"]]})
    omap, obs = {}, []
    for o in keep:
        omap[o["obs_id"]] = f"O{len(obs) + 1}"
        obs.append({**o, "obs_id": omap[o["obs_id"]], "source_ref": [smap[s] for s in o["source_ref"]]})
    return sources, obs, omap
