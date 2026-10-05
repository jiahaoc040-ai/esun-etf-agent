"""競賽規則常數（出處：比賽辦法 六、D-Plan 撰寫指南 v4.2）。改這裡的值＝改版，記得換 code_version。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = ROOT / "data" / "reference"
SCHEMA_PATH = Path(__file__).resolve().parent / "D-Plan.schema_v4.2.json"

INITIAL_CAPITAL = 1_000_000_000          # 初始 10 億虛擬本金
LOT_SIZE = 1000                          # 整股
FEE_RATE = 0.001425                      # 手續費，買賣皆收
TAX_RATE = 0.003                         # 證交稅，僅賣出

MIN_HOLDINGS = 20
MAX_HOLDINGS = 30
TSMC = "2330"
TSMC_MAX_WEIGHT = 0.25
STOCK_MAX_WEIGHT = 0.10
CASH_MIN = 0.0
CASH_MAX = 0.25                          # 現金需 < 25%

HOLD_TOLERANCE = 0.02                    # posture=hold 時 |淨流向| ≤ 2% NAV（server 預設參數）
ACTIVE_SHARE_MIN = 0.20                  # 前 10 大持股 vs 各主動型 ETF 前 10 大
ACTIVE_SHARE_TOP_N = 10

# 自訂安全緩衝（非官方規則，策略可調）
CASH_BUFFER_LOW = 0.01                   # 現金至少留 1%，避免以均價成交後變負
CASH_BUFFER_HIGH = 0.22                  # 現金上限留 3% 緩衝
WEIGHT_BUFFER = 0.005                    # 個股權重距上限留 0.5%


def max_weight(ticker: str) -> float:
    return TSMC_MAX_WEIGHT if ticker == TSMC else STOCK_MAX_WEIGHT
