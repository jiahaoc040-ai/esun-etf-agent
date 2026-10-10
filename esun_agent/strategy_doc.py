"""ETF 投資策略說明文件（docx）產生器。

版型沿用主辦方的「ETF 投資策略說明文件_參賽隊伍繳交格式_v1.docx」：第一頁三欄表單（ETF 名稱、投資主題、投資理念）
由 config/strategy_declaration.json 填入（名稱 ≤20 字且開頭須為「主動」、主題 ≤50 字、理念 100～300 字，不符就不產檔）。
表單之後是「補充說明」（form_only=True 時省略）：策略主體、超額來源、回測摘要、與每日 D-Plan 的對應。
所有文字內容都來自宣告檔與 docs/final_strategy_backtest.json，不另寫一份；數字隨回測結果更新。
"""
from __future__ import annotations

import json
from pathlib import Path

import docx
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from .config import ROOT
from .strategy.declaration import load_declaration, validate_submission

TEMPLATE = ROOT / "docs" / "competition" / "玉山挑戰賽_ETF投資策略說明文件_參賽隊伍繳交格式_v1.docx"
SUMMARY_PATH = ROOT / "docs" / "final_strategy_backtest.json"
OUTPUT_PATH = ROOT / "output" / "strategy_doc.docx"
FORM_FONT = "標楷體"
BODY_FONT = "微軟正黑體"
BLUE = RGBColor(0x1F, 0x4E, 0x79)

# 宣告 → 每日 D-Plan 的對應（observation.topic 與宣告檔的 tilt 依據 id 相同；程式在 esun_agent/agent/ 內檢查）
MARKET_TOPICS = [("tw_market", "台股整體（名單內 150 檔等權報酬、漲跌家數、成交值）"), ("inst_flow", "外資、投信買賣超金額"),
                 ("taifex_night", "台指期夜盤"), ("us_overnight", "美股／ADR 收盤")]


class DocError(ValueError):
    pass


def pct(x: float, d: int = 1, sign: bool = True) -> str:
    return f"{x * 100:+.{d}f}%" if sign else f"{x * 100:.{d}f}%"


def load_summary(path: Path | None = None) -> dict:
    p = path or SUMMARY_PATH
    if not p.exists():
        raise DocError(f"找不到回測摘要 {p}，請先執行 python scripts/run_final_backtest.py")
    return json.loads(p.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------- docx 小工具

def _font(run, name=BODY_FONT, size=None, bold=None, color=None):
    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.append(rf)
    for a in ("w:ascii", "w:hAnsi", "w:eastAsia"):
        rf.set(qn(a), name)
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if color is not None:
        run.font.color.rgb = color


def _shade(cell, fill):
    tcpr = cell._element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcpr.append(shd)


def heading(doc, text, level=1):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(14 if level == 1 else 8)
    p.paragraph_format.space_after = Pt(4)
    p.paragraph_format.keep_with_next = True
    _font(p.add_run(text), BODY_FONT, 14 if level == 1 else 11.5, True, BLUE)
    return p


def para(doc, text, *, bold=False, size=10, italic=False, color=None, after=4):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(after)
    r = p.add_run(text)
    _font(r, BODY_FONT, size, bold, color)
    r.italic = italic
    return p


def bullets(doc, items, size=10):
    for t in items:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Pt(14)
        p.paragraph_format.first_line_indent = Pt(-10)
        p.paragraph_format.space_after = Pt(2)
        _font(p.add_run("• " + t), BODY_FONT, size)


def table(doc, header, rows, widths=None, size=9):
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(header):
        c = t.rows[0].cells[i]
        c.text = ""
        _font(c.paragraphs[0].add_run(h), BODY_FONT, size, True, BLUE)
        _shade(c, "F2F6FA")
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = ""
            _font(cells[i].paragraphs[0].add_run(str(v)), BODY_FONT, size)
    if widths:
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Pt(w)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return t


def mapping_box(doc, lines: list[str]):
    """每段宣告結尾的「D-Plan 對應」框：說明這段宣告在每日 D-Plan 的哪些欄位出現。"""
    t = doc.add_table(rows=1, cols=1)
    t.style = "Table Grid"
    c = t.rows[0].cells[0]
    _shade(c, "F7F9FC")
    c.text = ""
    _font(c.paragraphs[0].add_run("對應每日 D-Plan"), BODY_FONT, 9, True, BLUE)
    for ln in lines:
        p = c.add_paragraph()
        p.paragraph_format.space_after = Pt(1)
        _font(p.add_run("• " + ln), BODY_FONT, 9)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


# ----------------------------------------------------------------------------- 表單

def _fill_form(doc, decl: dict) -> None:
    t = doc.tables[0]
    values = [decl["etf"]["name"], decl["theme"]["title"], decl["philosophy_statement"]]
    if len(t.rows) != 3:
        raise DocError("官方範本表單應為 3 列（名稱／主題／理念）")
    for row, v in zip(t.rows, values):
        cell = row.cells[1]
        p = cell.paragraphs[0]
        for r in list(p.runs):
            r._element.getparent().remove(r._element)
        _font(p.add_run(v), FORM_FONT, 10.5)


# ----------------------------------------------------------------------------- 補充說明各節

def _sec_core(doc, decl, S):
    r = decl["portfolio_rules"]
    heading(doc, "補充說明")
    para(doc, "以下內容補充說明上表的宣告，並標示每段宣告將如何出現在每日 D-Plan（market_view、inferences、decisions）。", size=9, italic=True)
    heading(doc, "一、主體：市值加權的合規基準", 2)
    para(doc, decl["benchmark"]["definition"], size=10)
    table(doc, ["項目", "規則"], [
        ["投資範圍", decl["etf"]["market"]],
        ["持股", f"基準取前 {r['n_base']} 檔；持股維持 {r['min_hold']}–30 檔（內部上限 {r['max_hold']} 檔，保留 1 檔給停牌賣不掉的部位）"],
        ["個股上限", f"基準：2330 ≤ {r['base_cap_tsmc']:.0%}、其他 ≤ {r['base_cap_other']:.0%}；加減碼後：2330 ≤ {r['tilt_cap_tsmc']:.0%}、其他 ≤ {r['tilt_cap_other']:.1%}（官方上限 25%／10%，留出價格漂移空間）"],
        ["現金", f"目標 {r['cash_target']:.0%}，區間 {r['cash_low']:.0%}–{r['cash_high']:.0%}；委託後預估現金比不得低於 2%（不足時按比例縮減買進）"],
        ["Active Share", f"與任一主動型 ETF 前 10 大持股的 Active Share 目標 ≥ {r['ap_target']:.0%}（官方門檻 20%）。基準權重不足時，先用最小調整法"
                          "（只動 1–3 檔重疊最大的標的，減下的權重分給第 11–28 名），不夠再逐步放寬"],
        ["Active Share 硬規則", f"前日收盤 Active Share < {r['ap_trigger']:.0%} → 當日必須再平衡到 ≥ {r['ap_target']:.0%}，不受無交易帶與 5 日週期限制；"
                                  f"前日收盤已經 < 20% → 當日補到 ≥ {r['ap_recovery_target']:.0%}，避免連續 2 日低於官方門檻"],
        ["漂移保護", f"前日收盤權重 2330 > {r['guard_tsmc']:.0%} 或其他個股 > {r['guard_other']:.0%} → 當日提前再平衡"],
        ["再平衡", f"每 {r['rebalance_every']} 個交易日一次；無交易帶 {r['band']:.0%}（權重差距在帶內不交易，避免往返約 0.6% 的成本）；"
                    "Agent 的 tilt 與前一日不同、或漂移保護觸發時，當天可以交易"],
    ], widths=[85, 380])
    mapping_box(doc, [
        "market_view：以名單整體表現（tw_market）、法人買賣超（inst_flow）、台指期夜盤（taifex_night）、美股／ADR（us_overnight）"
        "等市場級 observation 為 basis_refs，說明 regime／stance；posture 與現金區間由實際委託推得，不由 Agent 自填。",
        "inferences：再平衡日以「依合規基準（市值加權、2330 ≤ 20%、個股 ≤ 8%、取前 28 檔、Active Share ≥ 27%）重新計算目標權重，"
        "偏離無交易帶者才調整」為推論；非再平衡日或差距在無交易帶內者，以「基準權重未偏離無交易帶」為推論，並寫入 no_trade_decisions。",
        "decisions／orders：target_weight 為基準權重加上有效 tilt；委託一律由官方公式 derive_orders 機械推導，Agent 不決定股數。",
    ])


def _sec_alpha(doc, decl, S):
    r = decl["portfolio_rules"]
    heading(doc, "二、超額來源：Agent 依公開資訊的小幅加減碼（tilt）", 2)
    para(doc, "AI Agent 只在有具名公開資訊時，才在基準權重上做加碼或減碼；它不決定權重或股數，只提出『哪一檔、方向、依據、理由』，"
              "金額上限與所有風控由程式檢查。", size=10)
    table(doc, ["tilt 依據", "資料來源（authority）", "例子"],
          [[b["label"], "、".join(b["authority"]), "；".join(b["examples"])] for b in decl["tilt"]["allowed_bases"]], widths=[70, 90, 305])
    heading(doc, "紀律（程式強制，違反就退回重寫）", 2)
    bullets(doc, [
        f"單檔 |tilt| ≤ {r['max_delta']:.0%}；全部 tilt 的主動偏離 Σ|tilt|/2 ≤ {r['max_active']:.0%}；可加入基準前 28 名以外、投資範圍內的股票，但持股維持 20–30 檔。",
        "每筆 tilt 必須引用至少一則「該股、屬於所宣告依據、來源 authority 符合上表、且發布時間早於決策日 19:30」的 observation；禁止以股價走勢、傳聞或無來源消息為依據。",
        f"tilt 最短持有 {5} 個交易日：未滿期間只有「新事件且方向相反」才可提前撤銷、縮小或反向，不可同方向加碼；無新事件時一律沿用前一日 tilt。",
        "沒有足夠證據就不做 tilt（維持基準權重、寫入 no_trade_decisions）。",
        "tilt 套用後重新檢查單檔上限、現金、持股檔數與 Active Share；不合格時依序縮放、截斷或修正，必要時退回 Agent 重寫（最多 3 次，仍不合格則降級為零 tilt 的保底方案）。",
    ])
    heading(doc, "低回檔與紀律化如何落實", 2)
    bullets(doc, [
        "核心部位跟隨市值加權基準，tilt 的總偏離與單檔幅度都有明確上限，即使 Agent 判斷失誤，損失也有界。",
        "個股與 2330 的上限、3% 現金、委託後現金比 ≥ 2% 的檢查，限制了集中度與被迫賣出的風險。",
        "無交易帶與 5 日再平衡避免追價與過度交易；tilt 最短持有 5 日避免因單日雜訊來回切換。",
    ])
    mapping_box(doc, [
        "observations：每一種 tilt 依據在 D-Plan 的 topic 與依據 id 相同——" + "、".join(f"{b['id']}（{b['label']}）" for b in decl["tilt"]["allowed_bases"]) + "；來源 authority 如上表。",
        "inferences：每筆 tilt 一則推論，premise_refs 指向上述 observation，logic 說明『事實 → 為何支持加碼／減碼 → 與本 ETF 主題的關係』，"
        "counter_evidence 寫反方訊號；沿用中的 tilt 以「沿用 YYYY-MM-DD 起的○○ tilt ±x%…今日沒有新事件推翻」為推論，並重新引用原事件。",
        "decisions：有 tilt 的個股，decision 引用該 tilt 的推論；撤銷 tilt 時推論說明相反的新事件。",
        "market_view：regime／stance 須與實際操作方向相符（宣告 defensive 卻淨買超、或 aggressive 卻淨賣超會被退回）；counter_evidence 寫明降級條件。",
    ])


def _sec_factors(doc, decl, S):
    heading(doc, "主要選股因子", 2)
    table(doc, ["類別", "本 ETF 使用的因子"], [
        ["事件／資訊", "財報公告、月營收、法說會、重大訊息（以上來自公開資訊觀測站）"],
        ["籌碼", "投信連續買賣超（三大法人中僅用投信；證交所／櫃買中心公布）"],
        ["風險控管", "個股與 2330 權重上限、現金下限、Active Share 下限、tilt 總偏離上限、無交易帶、漂移保護"],
        ["基準", "市值（還原收盤價 × 發行股數）加權；市場級背景資訊（名單整體表現、法人、夜盤、美股）僅用於 market_view"],
    ], widths=[70, 395])
    para(doc, "本 ETF 不使用價格動能或技術指標作為 tilt 依據。", size=9, italic=True)
    mapping_box(doc, [
        "inferences：每筆 tilt 的推論引用上表因子對應的 observation（topic 即依據 id），conclusion 標明依據名稱（財報、月營收、法說會、投信連續買賣超、重大訊息）；"
        "沒有引用上述因子事實的 tilt 不會通過檢查，也不會出現在 decisions。",
        "market_view：市場級背景（名單整體、法人、夜盤、美股）只作為 regime／stance 的依據，不作為個股 tilt 的依據。",
    ])


def _sec_backtest(doc, decl, S):
    b = S["baseline_oos"]
    rt = S["random_tilt"]
    st = S["stress_daily_tilt"]
    bm = S["benchmarks_oos"]
    heading(doc, "三、回測摘要", 2)
    para(doc, f"資料期間 {S['universe_dates'][0]} ～ {S['universe_dates'][1]}。投資範圍 150 檔名單是 2026 年 7 月底以市值選出，"
              f"因此 {S['oos_start']} 之前的回測有前視與倖存者偏誤，只能用來比較參數的相對優劣；"
              f"本表只列樣本外（{S['oos_start']} 起，{S['oos_days']} 個交易日）。基準與策略皆含同樣的名單偏誤，"
              "所以看相對基準的差異比看絕對報酬可靠。", size=10)
    rows = [
        ["150 檔等權（基準 a）", pct(bm["a_equal_weight"]), pct(bm["a_mdd"]), "—", "—"],
        ["市值加權上限（基準 b）", pct(bm["b_cap_weight_capped"]), pct(bm["b_mdd"]), "—", "—"],
        ["本 ETF 基準（無 tilt）", pct(b["total_return"]), pct(b["mdd"]), pct(b["ap_min"], 1, False), f"{b['turnover_ann']:.1f}x"],
        ["本 ETF＋隨機 tilt（平均，模擬 Agent 雜訊 ×%d）" % rt["n"], pct(rt["ret"]["mean"]), pct(rt["mdd"]["mean"]),
         pct(rt["ap_min_all"]["min"], 1, False) + "（最低）", f"{rt['turnover']['mean']:.1f}x"],
        ["　└ 5%～95% 區間", f"{pct(rt['ret']['p5'])} ～ {pct(rt['ret']['p95'])}", f"最差 {pct(rt['mdd']['min'])}", "—", "—"],
    ]
    table(doc, ["樣本外", "報酬", "最大回檔", "Active Share 最低", "年換手"], rows, widths=[170, 95, 80, 75, 45])
    v = rt["violations"]
    bullets(doc, [
        f"隨機 tilt（每次 5 檔、±1～3%）沒有資訊含量，反映的是『Agent 判斷失誤的成本』：樣本外相對市值加權上限基準平均 {pct(rt['excess_vs_b']['mean'])}"
        f"（5%～95%：{pct(rt['excess_vs_b']['p5'])} ～ {pct(rt['excess_vs_b']['p95'])}），最大回檔平均 {pct(rt['mdd']['mean'])}，與基準相近，損失有界。",
        f"框架檢查：{rt['n']} 次模擬、每日期末檢查，持股檔數違規 {v['holdings']}、權重上限違規 {v['weight']}、現金比違規 {v['cash']}；"
        f"Active Share 單日低於 20% 共 {v['ap_below_20_days']} 天、最長連續 {v['ap_max_consecutive']} 日（官方取消資格條件為連續 2 日）。",
        f"壓力測試（tilt 每天都變，{st['n']} 次）：樣本外平均 {pct(st['ret']['mean'])}，年換手 {st['turnover']['mean']:.0f}x——tilt 頻繁變動的成本很高，"
        "因此規則要求最短持有 5 日、無新事件不變動。",
        "回測不證明 Agent 能創造超額報酬：真實 Agent 的 tilt 有資訊含量與否，要靠初賽期間的實際表現檢驗。樣本外僅 "
        f"{S['oos_days']} 個交易日，統計誤差大；Active Share 以 2026-10-06 的主動型 ETF 前 10 大持股快照近似；未計股利。",
    ], size=9.5)
    mapping_box(doc, [
        "market_view.counter_evidence 與 inferences.counter_evidence：誠實寫出反方訊號；信心（confidence）不得高於證據所能支持的程度。",
        "no_trade_decisions：沒有證據或差距在無交易帶內的持股一律寫入，並引用『基準權重未偏離無交易帶』的推論。",
    ])


def _sec_checklist(doc, decl, S):
    heading(doc, "四、每日 D-Plan 與本文件的對照檢查", 2)
    rows = [
        ["投資主題：市值加權核心", "market_view（basis_refs 引用 " + "、".join(t for t, _ in MARKET_TOPICS) + "）；inferences（再平衡／未偏離無交易帶）；decisions.target_weight", "assemble.build_doc"],
        ["投資理念：依公開資訊小幅加減碼", "observations.topic ∈ 五種 tilt 依據；inferences 引用該 observation；decisions 引用 inference", "assemble.check_proposal"],
        ["每筆 tilt 須有具名來源", "sources.authority 與 content_as_of；observations.source_ref；dplan_validate C1（引用鏈不可斷）", "validate()"],
        ["單檔 ±3%、總偏離 ≤ 15%、最短持有 5 日", "decisions.target_weight 與基準的差；沿用的 tilt 以『沿用…』推論重新引用原事件", "final.apply_tilts／tilt_state"],
        ["Active Share ≥ 27%（官方 20%）", "target_weight 已通過 Active Share 檢查才進入 decisions（不單獨列欄位）", "final.ensure_active_share"],
        ["現金與曝險承諾", "market_view.posture 由實際委託推得；dplan_validate C12", "plan.posture_for／validate()"],
        ["紀律：無證據不動", "no_trade_decisions 涵蓋所有未異動持股", "dplan_validate 覆蓋檢查"],
    ]
    table(doc, ["本文件的宣告", "每日 D-Plan 對應欄位", "程式檢查"], rows, widths=[120, 255, 90])
    para(doc, f"宣告版本 {decl['version']}（status = {decl['status']}）。{decl['status_note']}", size=8.5, italic=True)


def build_strategy_doc(out: Path | None = None, *, decl: dict | None = None, summary: dict | None = None,
                       template: Path | None = None, form_only: bool = False) -> Path:
    decl = decl or load_declaration()
    errs = validate_submission(decl)
    if errs:
        raise DocError("宣告檔不符官方格式限制：" + "；".join(errs))
    doc = docx.Document(str(template or TEMPLATE))
    _fill_form(doc, decl)
    if not form_only:
        S = summary or load_summary()
        _sec_core(doc, decl, S)
        _sec_alpha(doc, decl, S)
        _sec_factors(doc, decl, S)
        _sec_backtest(doc, decl, S)
        _sec_checklist(doc, decl, S)
    p = out or OUTPUT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(p))
    return p
