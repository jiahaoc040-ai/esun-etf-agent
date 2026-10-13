"""集中策略的狀態（runs/state/conviction.json）：高信心名單、進場日、信心等級、引用的證據、湊檔股。

- since：列入名單的交易日（最短持有 3 日、成本 = 該日成交均價都從這天算）；升／降級不改 since。
- evidence：Evidence.payload()（含來源），隔天重建成 carried observation，讓續抱的推論能重新引用原證據。
- 狀態只在 D-Plan 通過驗證並寫檔後才更新（write 由 assemble 端呼叫）。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import ROOT

STATE_PATH = ROOT / "runs" / "state" / "conviction.json"


@dataclass
class Pick:
    ticker: str
    level: str                          # high | medium
    since: str                          # 列入名單的交易日
    basis: str
    logic: str
    counter_evidence: str | None
    evidence: list[dict]
    evidence_as_of: str
    confidence: float | None = None


@dataclass
class ConvictionState:
    picks: dict[str, Pick] = field(default_factory=dict)
    fillers: list[str] = field(default_factory=list)
    as_of: str | None = None            # 最後一次成功寫入的交易日 T
    prev_date: str | None = None        # 該次的決策日 D（判斷「今日新事件」用）
    last_fallback: str | None = None
    mode: str | None = None             # conviction | hold | base

    @classmethod
    def load(cls, path: Path | None = None) -> "ConvictionState":
        p = path or STATE_PATH
        if not p.exists():
            return cls()
        d = json.loads(p.read_text(encoding="utf-8"))
        return cls(picks={t: Pick(**x) for t, x in d.get("picks", {}).items()}, fillers=list(d.get("fillers", [])),
                   as_of=d.get("as_of"), prev_date=d.get("prev_date"), last_fallback=d.get("last_fallback"),
                   mode=d.get("mode"))

    def save(self, path: Path | None = None) -> Path:
        p = path or STATE_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"as_of": self.as_of, "prev_date": self.prev_date, "mode": self.mode,
                                 "last_fallback": self.last_fallback, "fillers": self.fillers,
                                 "picks": {t: asdict(x) for t, x in self.picks.items()}},
                                ensure_ascii=False, indent=1), encoding="utf-8")
        return p
