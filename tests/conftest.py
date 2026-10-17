"""測試隔離：不讀本機真實 .env、data/etf_top15 最新檔、data/datapack、~/Downloads。

- .env：llm_budget.DOTENV_PATH 指到不存在的路徑（否則 SS 的 Mac 上 TEAM_ID 等會被 .env 帶入）。
- 主動型 ETF 前 15 大：固定用 tests/fixtures/etf_top15/（commit 進 repo 的快照），不受每日抓取的新檔影響。
- 資料包：DataPack 搜尋路徑指到空的暫存資料夾。
- 內部人申報累積檔（data/corporate/insider/）：指到暫存資料夾。
- FRED_API_KEY：清掉（本機 shell 若有設定，FRED 會改走正式 API，與 fredgraph.csv 的 fixture 不符）；需要時測試自行 setenv。
data/market 的歷史行情（commit 進 repo）仍是端到端測試的輸入；「本機沒有某日行情」的情境改用 Paths.market 指到暫存資料夾。
"""
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_local_files(tmp_path_factory, monkeypatch):
    from esun_agent import datapack
    from esun_agent.agent import llm_budget
    from esun_agent.data import etf_holdings
    empty = tmp_path_factory.mktemp("isolated")
    monkeypatch.setattr(llm_budget, "DOTENV_PATH", empty / "no.env")
    monkeypatch.setattr(etf_holdings, "ETF_TOP15_DIR", FIXTURES / "etf_top15")
    monkeypatch.setattr(datapack, "DATAPACK_DIR", empty / "datapack")
    monkeypatch.setattr(datapack, "DOWNLOADS_DIR", empty / "Downloads")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    from esun_agent.collect import corporate
    monkeypatch.setattr(corporate, "INSIDER_DIR", empty / "insider")       # 內部人申報累積檔不寫進 repo
