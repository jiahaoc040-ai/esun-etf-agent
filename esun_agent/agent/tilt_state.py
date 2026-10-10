"""tilt 狀態（runs/state/tilts.json）與持有規則。

規則：
  - 無新事件 → 沿用前一日 tilt（狀態檔裡的 tilt 每天自動帶進來，LLM 沒提到的 tilt 一律不變）。
  - 任何變更（新增、加減幅度、撤銷）都要引用「新事件」：該事件的 content_as_of 必須晚於這檔現有 tilt 最近一次引用的證據。
  - 最短持有 MIN_HOLD_DAYS（5）個交易日：持有未滿 5 日時，只有「新事件且方向與現有 tilt 相反」才可變更
    （撤銷、縮小、反向），且不可同方向加碼。
  - 新增或變更後，持有天數從該交易日重新起算（since）。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import ROOT

MIN_HOLD_DAYS = 5
STATE_PATH = ROOT / "runs" / "state" / "tilts.json"


@dataclass
class ActiveTilt:
    ticker: str
    delta: float
    since: str                        # 這個 tilt 生效的交易日（最短持有從這天起算）
    basis: str                        # 五種依據 id 之一
    logic: str
    counter_evidence: str | None
    evidence: list[dict]              # Evidence.payload()：舊事件含來源，可重建成當天的 observation
    evidence_as_of: str               # 證據中最晚的 content_as_of
    confidence: float | None = None


@dataclass
class TiltState:
    tilts: dict[str, ActiveTilt] = field(default_factory=dict)
    last_rebalance: str | None = None
    as_of: str | None = None          # 最後一次成功寫入的交易日
    last_fallback: str | None = None

    def deltas(self) -> dict[str, float]:
        return {t: a.delta for t, a in self.tilts.items()}

    @classmethod
    def load(cls, path: Path | None = None) -> "TiltState":
        p = path or STATE_PATH
        if not p.exists():
            return cls()
        d = json.loads(p.read_text(encoding="utf-8"))
        return cls(tilts={t: ActiveTilt(**a) for t, a in d.get("tilts", {}).items()},
                   last_rebalance=d.get("last_rebalance"), as_of=d.get("as_of"), last_fallback=d.get("last_fallback"))

    def save(self, path: Path | None = None) -> Path:
        p = path or STATE_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"as_of": self.as_of, "last_rebalance": self.last_rebalance,
                                 "last_fallback": self.last_fallback,
                                 "tilts": {t: asdict(a) for t, a in sorted(self.tilts.items())}},
                                ensure_ascii=False, indent=1), encoding="utf-8")
        return p


def trading_days_elapsed(calendar: list[str], since: str, today: str) -> int:
    """(since, today] 之間的交易日數。calendar 是已知交易日（到決策日 D 為止）；today 尚不在其中時視為下一個交易日。"""
    cal = list(calendar)
    if today not in cal:
        cal.append(today)
    if since not in cal:
        return 0
    return cal.index(today) - cal.index(since)


def sign(x: float) -> int:
    return (x > 1e-12) - (x < -1e-12)


def check_change(existing: ActiveTilt | None, new_delta: float, event_direction: str | None, new_event: bool,
                 calendar: list[str], today: str) -> list[str]:
    """檢查一筆 tilt 變更是否合規。回傳錯誤訊息（空 = 合規）。new_event = 是否引用了晚於現有證據的新事件。
    event_direction：LLM 宣告所引新事件對該股的方向（positive／negative）。"""
    errs: list[str] = []
    if existing is None:
        if sign(new_delta) == 0:
            return ["沒有現有 tilt 可撤銷"]
        if not new_event:
            errs.append("新增 tilt 需要引用新事件")
        if event_direction and (event_direction == "positive") != (new_delta > 0):
            errs.append("事件方向與 delta 方向不一致（positive 事件對應加碼、negative 對應減碼）")
        return errs
    if abs(new_delta - existing.delta) < 1e-12:
        return []                                             # 沒有變更 → 沿用
    t = existing.ticker
    if not new_event:
        errs.append(f"{t}：無新事件，須沿用前一日 tilt（{existing.delta:+.2%}），不可變更")
        return errs
    held = trading_days_elapsed(calendar, existing.since, today)
    if held < MIN_HOLD_DAYS:
        # 只有方向相反的新事件可提前變更；變更後必須往反方向（撤銷／縮小／反向），不可同方向加碼
        opposite = (event_direction == "negative") if existing.delta > 0 else (event_direction == "positive")
        if not opposite:
            errs.append(f"{t}：tilt 自 {existing.since} 起只持有 {held} 個交易日（最短 {MIN_HOLD_DAYS}），"
                        f"提前變更需要方向相反的新事件（現有 {existing.delta:+.2%}，你宣告的事件方向 {event_direction}）")
        if sign(new_delta) == sign(existing.delta) and abs(new_delta) > abs(existing.delta):
            errs.append(f"{t}：持有未滿 {MIN_HOLD_DAYS} 日不可同方向加碼")
    else:
        if sign(new_delta) != 0 and event_direction and (event_direction == "positive") != (new_delta > existing.delta):
            errs.append(f"{t}：事件方向與 tilt 變動方向不一致")
    return errs
