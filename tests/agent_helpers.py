"""T4 測試用：腳本化的假 LLM、從 prompt 取 obs_id、以 Ledger 模擬主辦方結算的連續交易日。"""
import json
import re
from pathlib import Path

from esun_agent.agent.assemble import DayContext, assemble_day, write_dplan
from esun_agent.agent.llm import LLMResult
from esun_agent.agent.tilt_state import TiltState
from esun_agent.ledger import Ledger

FX = Path(__file__).parent / "fixtures" / "agent"


def load_inputs(trade_date: str) -> dict:
    return json.loads((FX / f"inputs_{trade_date}.json").read_text(encoding="utf-8"))


def obs_in_prompt(user: str) -> list[dict]:
    out = []
    for line in user.splitlines():
        if line.startswith('{"obs_id"'):
            out.append(json.loads(line))
    return out


def find_obs(user: str, *, ticker=None, topic=None, carried=None) -> str:
    for o in obs_in_prompt(user):
        if (ticker is None or o["ticker"] == ticker) and (topic is None or o["topic"] == topic) \
                and (carried is None or o["carried"] == carried):
            return o["obs_id"]
    raise KeyError((ticker, topic))


def _optional(user: str, topic: str) -> list[str]:
    """美股等來源可能缺值（收集失敗）：有才引用。"""
    try:
        return [find_obs(user, topic=topic)]
    except KeyError:
        return []


def mv(user: str, regime="neutral", stance="neutral") -> dict:
    return {"regime": regime, "stance": stance,
            "logic": "台股整體溫和、法人買賣超互見、美股半導體小幅回檔且夜盤小跌，訊號混雜，判定中性，維持核心部位。",
            "basis_refs": [find_obs(user, topic="tw_market")] + _optional(user, "us_overnight"),
            "counter_evidence": "若美股半導體連跌兩日且夜盤續弱，將降為 risk_off", "confidence": 0.6}


class FakeLLM:
    """responses：依序回傳；每個元素是 str 或 callable(system, user) -> str。用完後重複最後一個。"""
    provider = "other"
    model = "fake-llm"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def generate(self, system, user, **kw):
        self.calls.append((system, user))
        r = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        text = r(system, user) if callable(r) else r
        return LLMResult(text, 1000, 200)


class Sim:
    """用 Ledger 模擬主辦方結算：每天依 D-Plan 的委託以 T 日均價成交，收盤估值。"""

    def __init__(self, env, tmp_path, capital=1e9, team="TEAM_042"):
        self.env, self.tmp, self.team = env, tmp_path, team
        self.ledger = Ledger(cash=float(capital))
        self.nav = float(capital)
        self.state_path = tmp_path / "state" / "tilts.json"
        self.results = []

    def day(self, trade_date: str, llm, **kw):
        panel = self.env["panel"]
        i = panel.dates.index(trade_date)
        D = panel.dates[i - 1]
        ctx = DayContext(team_id=self.team, trade_date=trade_date, prev_date=D, panel=panel, factors=self.env["factors"],
                         caps=self.env["caps"], top15=self.env["top15"], prev_nav=self.nav, cash=self.ledger.cash,
                         holdings=dict(self.ledger.holdings), inputs=load_inputs(trade_date))
        state = TiltState.load(self.state_path)
        res = assemble_day(ctx, llm, state, **kw)
        write_dplan(res, self.tmp / "out", self.state_path)
        avg = panel.avg_price.loc[trade_date]
        fills = [{"ticker": o["ticker"], "side": o["side"], "shares": o["shares"]} for o in res.doc["orders"]]
        self.ledger.apply_fills(fills, {f["ticker"]: float(avg[f["ticker"]]) for f in fills})
        close = {t: float(v) for t, v in panel.close.loc[trade_date].dropna().items()}
        self.nav = float(self.ledger.nav({t: close[t] for t in self.ledger.holdings}))
        self.results.append(res)
        return res


# ------------------------------------------------------------------ 集中策略（v2）
def prompt_current_picks(user: str) -> list[tuple[str, str]]:
    return re.findall(r"^- (\d{4}) .*?信心（(high|medium)）", user, re.M)


def prompt_candidates(user: str) -> list[str]:
    return re.findall(r"^- 第 \d+ 名 (\d{4})", user, re.M)


def conviction_llm(n=11, n_high=5, drop=(), extra=(), keep_levels=True, regime="neutral", stance="neutral",
                   exit_with_fact=True):
    """依 prompt 自動產生合規的 picks：先續抱目前名單（drop 內的改列 exits），再依候選名次補到 n 檔（需有 trust_flow 證據）。
    extra：額外硬塞進 picks 的 (ticker, level)（用來製造違規）。"""
    def gen(system, user):
        obs = obs_in_prompt(user)
        trust = {o["ticker"]: o["obs_id"] for o in obs if o["topic"] == "trust_flow" and not o["carried"]}
        carried = {}
        for o in obs:
            if o["carried"] and o["ticker"]:
                carried.setdefault(o["ticker"], []).append(o["obs_id"])
        pos = {o["ticker"]: o["obs_id"] for o in obs if o["topic"] == "position_pnl"}
        picks, exits = [], []
        for t, lv in prompt_current_picks(user):
            if t in drop:
                ids = ([trust[t]] if exit_with_fact and t in trust else []) + [pos[t]]
                exits.append({"ticker": t, "obs_ids": ids, "logic": "投信買超轉弱、事件利多已反映於股價，移出高信心名單換入新證據更強的標的。"})
                continue
            ids = [trust[t]] if t in trust else carried.get(t, [])[:1]
            picks.append({"ticker": t, "conviction": lv if keep_levels else "medium", "basis": "trust_flow", "obs_ids": ids,
                          "logic": "投信持續買超且占成交值比重高，籌碼面證據仍在，依紀律續抱。", "counter_evidence": None,
                          "confidence": 0.5})
        for t, lv in extra:
            picks.append({"ticker": t, "conviction": lv, "basis": "trust_flow", "obs_ids": [trust.get(t, "O1")],
                          "logic": "測試用：硬塞進名單的股票，用來檢查換股紀律是否會退回。", "counter_evidence": None})
        held = {p["ticker"] for p in picks} | set(drop)
        for t in prompt_candidates(user):
            if len(picks) >= n:
                break
            if t in held or t not in trust:
                continue
            picks.append({"ticker": t, "conviction": "high" if len(picks) < n_high else "medium", "basis": "trust_flow",
                          "obs_ids": [trust[t]], "logic": "投信近 10 日淨買超占成交值比重高、候選分數前段，列入高信心名單。",
                          "counter_evidence": "籌碼面訊號可能短期反轉", "confidence": 0.5})
        return json.dumps({"market_view": mv(user, regime, stance), "picks": picks, "exits": exits}, ensure_ascii=False)
    return gen


class ConvSim(Sim):
    """集中策略版的連續交易日模擬（主辦方結算用 Ledger 代替）。"""

    def __init__(self, env, tmp_path, **kw):
        super().__init__(env, tmp_path, **kw)
        self.state_path = tmp_path / "state" / "conviction.json"

    def day(self, trade_date: str, llm, extra_events=(), datapack=None, **kw):
        from esun_agent.agent.conviction_agent import assemble_conviction_day, write_conviction_dplan
        from esun_agent.agent.conviction_state import ConvictionState
        panel = self.env["panel"]
        i = panel.dates.index(trade_date)
        ctx = DayContext(team_id=self.team, trade_date=trade_date, prev_date=panel.dates[i - 1], panel=panel,
                         factors=self.env["factors"], caps=self.env["caps"], top15=self.env["top15"], prev_nav=self.nav,
                         cash=self.ledger.cash, holdings=dict(self.ledger.holdings), inputs=load_inputs(trade_date),
                         datapack=datapack)
        if extra_events:
            ctx.inputs = {**ctx.inputs, "events": list(ctx.inputs.get("events", [])) + list(extra_events)}
        kw.setdefault("events", [])
        res = assemble_conviction_day(ctx, llm, ConvictionState.load(self.state_path), **kw)
        write_conviction_dplan(res, self.tmp / "out", self.state_path)
        avg = panel.avg_price.loc[trade_date]
        fills = [{"ticker": o["ticker"], "side": o["side"], "shares": o["shares"]} for o in res.doc["orders"]]
        self.ledger.apply_fills(fills, {f["ticker"]: float(avg[f["ticker"]]) for f in fills})
        close = {t: float(v) for t, v in panel.close.loc[trade_date].dropna().items()}
        self.nav = float(self.ledger.nav({t: close[t] for t in self.ledger.holdings}))
        self.results.append(res)
        return res

    def weights(self, trade_date: str) -> dict[str, float]:
        close = self.env["panel"].close.loc[trade_date]
        return {t: n * float(close[t]) / self.nav for t, n in self.ledger.holdings.items()}
