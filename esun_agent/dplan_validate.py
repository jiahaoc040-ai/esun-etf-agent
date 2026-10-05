"""D-Plan 提交前自我驗證（結構層 schema + 語意層 C 檢查的自製版本）。

主辦方若釋出 verify_dplan.py，以官方為準；這裡的檢查依「撰寫指南 v4.2」與官方正/反例反推。
嚴重度：
  ERROR = 主辦方會拒收或記警告 → 不得提交
  WARN  = 自訂安全緩衝或無法完整驗證 → 建議處理

context（提交當天的帳務狀態，持股必須來自主辦方後台結算庫存）：
  {
    "prev_nav": 1000000000,                 # 前日結算 NAV
    "cash": 12345678,                       # 前日結算現金
    "holdings": {"2330": 17000, ...},       # 前日結算持股（股數）
    "prev_close": {"2330": 1480, ...}       # 交易所公布之前日收盤價（需涵蓋持股與決策標的）
  }
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import (CASH_BUFFER_HIGH, CASH_BUFFER_LOW, CASH_MAX, HOLD_TOLERANCE, MAX_HOLDINGS,
                     MIN_HOLDINGS, SCHEMA_PATH, WEIGHT_BUFFER, max_weight)
from .orders import derive_orders, estimate_flows
from .universe import in_universe

LAYERS = [("sources", "source_id", "S"), ("observations", "obs_id", "O"),
          ("inferences", "inf_id", "I"), ("decisions", "decision_id", "D")]
FILENAME_RE = re.compile(r"^D-Plan_([A-Za-z0-9_-]+)_(\d{4}-\d{2}-\d{2})\.json$")


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def err(self, code, msg):
        self.errors.append(f"[{code}] {msg}")

    def warn(self, code, msg):
        self.warnings.append(f"[{code}] {msg}")

    def __str__(self):
        lines = [f"{'PASS' if self.ok else 'FAIL'}：{len(self.errors)} error / {len(self.warnings)} warning"]
        lines += [f"  ERROR {e}" for e in self.errors]
        lines += [f"  WARN  {w}" for w in self.warnings]
        return "\n".join(lines)


# ---------------------------------------------------------------- 結構層
def check_schema(doc: dict, rep: Report):
    try:
        import jsonschema
    except ImportError:
        rep.warn("SCHEMA", "未安裝 jsonschema，略過結構驗證（pip install jsonschema）")
        return
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    for e in sorted(v.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in e.absolute_path) or "(root)"
        rep.err("SCHEMA", f"{path}: {e.message[:200]}")


def check_filename(filename: str | None, doc: dict, rep: Report):
    if not filename:
        return
    m = FILENAME_RE.match(Path(filename).name)
    if not m:
        rep.err("FILENAME", f"檔名不符 D-Plan_<team_id>_<trade_date>.json：{filename}")
        return
    if m.group(1) != doc.get("team_id") or m.group(2) != doc.get("trade_date"):
        rep.err("FILENAME", f"檔名 team_id/trade_date 與 JSON 內容不一致：{filename}")


# ---------------------------------------------------------------- ID 與引用鏈
def check_ids(doc: dict, rep: Report) -> dict[str, set]:
    ids = {}
    for layer, key, prefix in LAYERS:
        seq = [x.get(key) for x in doc.get(layer, [])]
        ids[prefix] = set(seq)
        if len(seq) != len(set(seq)):
            rep.err("ID", f"{layer} 有重複 id")
        expected = [f"{prefix}{i}" for i in range(1, len(seq) + 1)]
        if seq != expected:
            rep.err("ID", f"{layer} 編號須從 {prefix}1 連續遞增、不跳號、無前導零；實際 {seq}")
    return ids


def check_refs(doc: dict, ids: dict, rep: Report):
    def need(refs, prefix, where):
        for r in refs:
            if r not in ids[prefix]:
                rep.err("C1", f"{where} 引用不存在的 {r}")

    for o in doc.get("observations", []):
        need(o.get("source_ref", []), "S", o.get("obs_id"))
    need(doc.get("market_view", {}).get("basis_refs", []), "O", "market_view")
    for i in doc.get("inferences", []):
        need(i.get("premise_refs", []), "O", i.get("inf_id"))
    for d in doc.get("decisions", []):
        need(d.get("inference_refs", []), "I", d.get("decision_id"))
        need(d.get("funding_for", []), "D", f"{d.get('decision_id')}.funding_for")
    for n in doc.get("no_trade_decisions", []):
        need(n.get("reason_refs", []), "I", f"no_trade {n.get('ticker')}")
    for o in doc.get("orders", []):
        need([o.get("decision_ref")], "D", f"order {o.get('ticker')}")

    # 未被引用的節點（不一定拒收，但通常代表斷環或冗餘）
    used_s = {s for o in doc.get("observations", []) for s in o.get("source_ref", [])}
    used_o = set(doc.get("market_view", {}).get("basis_refs", [])) | {
        p for i in doc.get("inferences", []) for p in i.get("premise_refs", [])}
    used_i = {r for d in doc.get("decisions", []) for r in d.get("inference_refs", [])} | {
        r for n in doc.get("no_trade_decisions", []) for r in n.get("reason_refs", [])}
    for name, all_ids, used in (("source", ids["S"], used_s), ("observation", ids["O"], used_o),
                                ("inference", ids["I"], used_i)):
        unused = sorted(all_ids - used, key=lambda x: int(x[1:]))
        if unused:
            rep.warn("CHAIN", f"未被下層引用的 {name}: {unused}")


# ---------------------------------------------------------------- 決策覆蓋與動作
def check_decisions(doc: dict, ctx: dict, rep: Report):
    holdings = {t: n for t, n in ctx["holdings"].items() if n}
    decs, nts = doc.get("decisions", []), doc.get("no_trade_decisions", [])
    dec_t = [d["ticker"] for d in decs]
    nt_t = [n["ticker"] for n in nts]
    for name, lst in (("decisions", dec_t), ("no_trade_decisions", nt_t)):
        dup = {t for t in lst if lst.count(t) > 1}
        if dup:
            rep.err("COVER", f"{name} 同一檔重複：{sorted(dup)}")
    both = set(dec_t) & set(nt_t)
    if both:
        rep.err("COVER", f"同一檔同時出現在 decisions 與 no_trade_decisions：{sorted(both)}")
    missing = set(holdings) - set(dec_t) - set(nt_t)
    if missing:
        rep.err("COVER", f"昨日持股未交代（需在 decisions 或 no_trade_decisions 擇一）：{sorted(missing)}")
    for t in nt_t:
        if t not in holdings:
            rep.err("COVER", f"no_trade_decisions 的 {t} 並非昨日持股")

    for d in decs:
        t, a, w, held = d["ticker"], d["action"], d["target_weight"], holdings.get(d["ticker"], 0)
        if not in_universe(t):
            rep.err("UNIVERSE", f"{d['decision_id']} {t} 不在 150 檔名單")
        if w > max_weight(t):
            rep.err("C9", f"{d['decision_id']} {t} target_weight {w} 超過上限 {max_weight(t)}")
        elif w > max_weight(t) - WEIGHT_BUFFER:
            rep.warn("C9", f"{d['decision_id']} {t} target_weight {w} 距上限不足 {WEIGHT_BUFFER}")
        if a == "BUY" and held:
            rep.err("ACTION", f"{d['decision_id']} {t} action=BUY 僅限新建倉，但已持有 {held} 股（應為 ADD）")
        if a in ("ADD", "TRIM", "SELL_ALL") and not held:
            rep.err("ACTION", f"{d['decision_id']} {t} action={a} 但目前未持有")
        if a == "SELL_ALL" and w != 0:
            rep.err("ACTION", f"{d['decision_id']} {t} SELL_ALL 的 target_weight 應為 0")
        if a in ("BUY", "ADD", "TRIM") and w == 0:
            rep.err("ACTION", f"{d['decision_id']} {t} action={a} 但 target_weight=0（應為 SELL_ALL）")
        if "funding_for" in d and a not in ("TRIM", "SELL_ALL"):
            rep.err("C14", f"{d['decision_id']} funding_for 只能用在 TRIM/SELL_ALL")


# ---------------------------------------------------------------- 委託與部位
def check_orders(doc: dict, ctx: dict, rep: Report):
    nav, close, holdings = ctx["prev_nav"], ctx["prev_close"], ctx["holdings"]
    decs = doc.get("decisions", [])
    missing_px = sorted({d["ticker"] for d in decs} - set(close))
    if missing_px:
        rep.err("CTX", f"context.prev_close 缺少決策標的價格：{missing_px}")
        return

    expected = [o.to_dict() for o in derive_orders(decs, nav, close, holdings)]
    key = lambda o: (o["ticker"], o["side"], o["shares"], o["decision_ref"])
    actual = doc.get("orders", [])
    if sorted(map(key, actual)) != sorted(map(key, expected)):
        exp = {o["decision_ref"]: key(o) for o in expected}
        act = {o["decision_ref"]: key(o) for o in actual}
        for ref in sorted(set(exp) | set(act), key=lambda x: int(x[1:])):
            if exp.get(ref) != act.get(ref):
                rep.err("C2", f"{ref} 委託 {act.get(ref)} ≠ 官方公式推導 {exp.get(ref)}")

    # C13 超賣
    for o in actual:
        if o["side"] == "SELL" and o["shares"] > holdings.get(o["ticker"], 0):
            rep.err("C13", f"{o['ticker']} 賣出 {o['shares']} 股 > 持有 {holdings.get(o['ticker'], 0)} 股（整份拒收）")

    # C12 posture 一致性（前日收盤價估算、不含費稅）
    mv = doc.get("market_view", {})
    posture = mv.get("posture", {})
    intent = posture.get("net_exposure_intent")
    lo, hi = (posture.get("target_cash_pct_range") or [0, 0.25])[:2]
    if lo > hi:
        rep.err("C12", f"target_cash_pct_range 下限 {lo} > 上限 {hi}")
    buy, sell = estimate_flows(actual, close)
    net = buy - sell
    if intent == "increase" and not net > 0:
        rep.err("C12", f"宣告 increase 但委託淨流向 {net:,.0f}（需淨買超）")
    if intent == "reduce" and not net < 0:
        rep.err("C12", f"宣告 reduce 但委託淨流向 {net:,.0f}（需淨賣超）")
    if intent == "hold" and abs(net) > HOLD_TOLERANCE * nav:
        rep.err("C12", f"宣告 hold 但 |淨流向| {abs(net):,.0f} > {HOLD_TOLERANCE:.0%} NAV")
    cash_after = ctx["cash"] - net
    cash_pct = cash_after / nav
    if not (lo <= cash_pct <= hi):
        rep.err("C12", f"預估委託後現金比 {cash_pct:.2%} 不在宣告區間 [{lo:.2%}, {hi:.2%}]")
    if lo < CASH_BUFFER_LOW or hi > CASH_BUFFER_HIGH:
        rep.warn("C12", f"現金區間 [{lo}, {hi}] 未對 0%/25% 留緩衝（建議 [{CASH_BUFFER_LOW}, {CASH_BUFFER_HIGH}] 內）")

    # C9/C11 委託後部位（以前日收盤估算）
    post = dict(holdings)
    for o in actual:
        post[o["ticker"]] = post.get(o["ticker"], 0) + (o["shares"] if o["side"] == "BUY" else -o["shares"])
    post = {t: n for t, n in post.items() if n > 0}
    if not (MIN_HOLDINGS <= len(post) <= MAX_HOLDINGS):
        rep.err("C11", f"委託後持股 {len(post)} 檔，不在 {MIN_HOLDINGS}–{MAX_HOLDINGS} 檔")
    if cash_after < 0:
        rep.err("C9", f"預估委託後現金為負 {cash_after:,.0f}")
    elif cash_pct >= CASH_MAX:
        rep.err("C9", f"預估委託後現金比 {cash_pct:.2%} ≥ 25%")
    no_px = sorted(set(post) - set(close))
    if no_px:
        rep.warn("CTX", f"缺少持股收盤價，無法檢查權重：{no_px}")
    traded = {o["ticker"]: o["side"] for o in actual}
    for t, n in post.items():
        if t in close:
            w = n * close[t] / nav
            if w > max_weight(t):
                # 註 3：因加碼超限當日記警告；因價格波動超限有 5 個交易日緩衝
                level = rep.err if traded.get(t) == "BUY" else rep.warn
                level("C9", f"{t} 預估權重 {w:.2%} > 上限 {max_weight(t):.0%}"
                      + ("（加碼造成，當日記警告）" if traded.get(t) == "BUY" else "（價格波動，須 5 日內減碼）"))
        if not in_universe(t):
            rep.err("UNIVERSE", f"持股 {t} 不在 150 檔名單")

    # C14 funding_for 缺口存在性
    dmap = {d["decision_id"]: d for d in decs}
    funders = [d for d in decs if d.get("funding_for")]
    for d in funders:
        for ref in d["funding_for"]:
            if ref in dmap and dmap[ref]["action"] not in ("BUY", "ADD"):
                rep.err("C14", f"{d['decision_id']}.funding_for 指向 {ref}，但它不是 BUY/ADD")
    if funders:
        fund_ids = {d["decision_id"] for d in funders}
        sells_removed = sum(o["shares"] * close[o["ticker"]] for o in actual
                            if o["side"] == "SELL" and o["decision_ref"] in fund_ids)
        cash_wo = (cash_after - sells_removed) / nav
        if not cash_wo < lo:
            rep.err("C14", f"拿掉調度賣出後現金比 {cash_wo:.2%} 仍 ≥ 下限 {lo:.2%}：資金缺口不存在，不應標 funding_for")


# ---------------------------------------------------------------- 時間
def check_times(doc: dict, rep: Report, submit_at: datetime | None):
    md = doc.get("agent_metadata", {})
    try:
        s = datetime.fromisoformat(md["run_started_at"])
        c = datetime.fromisoformat(md["run_completed_at"])
    except (KeyError, ValueError):
        return
    if s > c:
        rep.err("C6", "run_started_at 晚於 run_completed_at")
    if submit_at and c > submit_at:
        rep.err("C6", "run_completed_at 晚於提交時間")
    for src in doc.get("sources", []):
        if "fetched_at" in src and submit_at and datetime.fromisoformat(src["fetched_at"]) > submit_at:
            rep.err("C6", f"{src['source_id']} fetched_at 晚於提交時間")


# ---------------------------------------------------------------- 入口
def validate(doc: dict, ctx: dict | None = None, filename: str | None = None,
             submit_at: datetime | None = None) -> Report:
    rep = Report()
    check_schema(doc, rep)
    check_filename(filename, doc, rep)
    ids = check_ids(doc, rep)
    check_refs(doc, ids, rep)
    check_times(doc, rep, submit_at)
    if ctx is None:
        rep.warn("CTX", "未提供帳務 context，略過 C2/C9/C11/C12/C13/C14 與覆蓋檢查")
    else:
        check_decisions(doc, ctx, rep)
        check_orders(doc, ctx, rep)
    return rep
