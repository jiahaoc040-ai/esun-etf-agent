"""競賽規則常數（出處：比賽辦法 六、D-Plan 撰寫指南 v4.2、schema v4.3、DataPack 規格書 v4.3）。改這裡的值＝改版，記得換 code_version。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = ROOT / "data" / "reference"
SCHEMA_VERSION = "4.3"                   # 輸出的 schema_version；驗證用同版 schema
SCHEMA_PATH = Path(__file__).resolve().parent / f"D-Plan.schema_v{SCHEMA_VERSION}.json"

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
CASH_MAX = 0.25                          # 現金不超過 25%（≤；1005 版辦法六(九)）

HOLD_TOLERANCE = 0.02                    # posture=hold 時 |淨流向| ≤ 2% NAV（server 預設參數）
ACTIVE_SHARE_MIN = 0.15                  # 我方前 15 大 vs 各主動型 ETF 前 15 大；< 15% 違規（1005 版辦法六(十三)）
ACTIVE_SHARE_TOP_N = 15

# 自訂安全緩衝（非官方規則，策略可調）
CASH_BUFFER_LOW = 0.01                   # 現金至少留 1%，避免以均價成交後變負
CASH_BUFFER_HIGH = 0.22                  # 現金上限留 3% 緩衝
WEIGHT_BUFFER = 0.005                    # 個股權重距上限留 0.5%

# 合規保險（自訂）
PASSIVE_SOFT_LIMIT = 0.095               # 前日結算權重（nav_tradable、T-1 收盤）> 9.5% → 強制 TRIM（被動超限 5 日內須調整）
PASSIVE_SOFT_LIMIT_TSMC = 0.24           # 2330 > 24% → 強制 TRIM
LIMIT_UP = 0.10                          # 當日加碼壓力測試：T-1 收盤 × 1.10（漲停）後權重仍須 ≤ 上限


def max_weight(ticker: str) -> float:
    return TSMC_MAX_WEIGHT if ticker == TSMC else STOCK_MAX_WEIGHT


def passive_soft_limit(ticker: str) -> float:
    return PASSIVE_SOFT_LIMIT_TSMC if ticker == TSMC else PASSIVE_SOFT_LIMIT


def stress_cap(ticker: str) -> float:
    """BUY/ADD 的目標權重上限：漲停（×1.10）後仍 ≤ 個股上限。"""
    return max_weight(ticker) / (1 + LIMIT_UP)
