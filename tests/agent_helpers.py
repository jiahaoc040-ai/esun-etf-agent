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


def mv(user: str, regime="neutral", stance="neutral") -> dict:
    return {"regime": regime, "stance": stance,
            "logic": "台股整體溫和、法人買賣超互見、美股半導體小幅回檔且夜盤小跌，訊號混雜，判定中性，維持核心部位。",
            "basis_refs": [find_obs(user, topic="tw_market"), find_obs(user, topic="us_overnight")],
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
                         caps=self.env["caps"], top10=self.env["top10"], prev_nav=self.nav, cash=self.ledger.cash,
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
