# launchd 排程（macOS）

| 工作 | 時間 | 指令 |
|---|---|---|
| `tw.esun.dplan.evening` | **週日～週四** 20:30 | `python scripts/daily_run.py` |
| `tw.esun.dplan.morning` | 週一～週五 06:50 | `python scripts/daily_run.py --morning` |

為什麼晚上是「週日～週四」而不是平日：D-Plan 要在**交易日的前一天晚上**產生（繳交時間：前一日 19:30～當日 08:55）。
週一的 D-Plan 在週日晚上產生；週五晚上跑也只會印「下一個交易日不是明天」然後結束。
國定假日由程式依 TWSE 休市日判斷（明天不是交易日就不產生、今天不是交易日的清晨就不檢查），排程不用改。

## 安裝／移除

```bash
bash scripts/launchd/install.sh      # 安裝並載入（使用 ./.venv/bin/python）
bash scripts/launchd/uninstall.sh    # 移除
launchctl kickstart -k gui/$(id -u)/tw.esun.dplan.evening   # 立即試跑一次
tail -f runs/launchd/evening.out.log
```

輸出：`runs/<交易日>/D-Plan_<TEAM_ID>_<交易日>.json`；每次執行的完整 log 在 `runs/<交易日>/raw/daily_run.log`（清晨為 `morning.log`），
launchd 的 stdout/stderr 在 `runs/launchd/`。

## 讓 Mac 在排程時間醒著（pmset）

launchd 在睡眠中錯過的排程，會在**醒來後**補跑一次——但如果醒來時已超過繳交截止（08:55）就沒有意義了。建議：

1. **接電源、不睡眠（最簡單）**：系統設定 → 電池 → 電源轉換器 →「防止 Mac 在顯示器關閉時自動進入睡眠」打開；
   或終端機 `sudo pmset -c sleep 0`（只影響接電源時）。螢幕可以關。
2. **定時喚醒**：`pmset repeat` 只能設定一個喚醒時間，所以設在清晨，晚上靠「接電源不睡眠」：
   ```bash
   sudo pmset repeat wakeorpoweron MTWRFSU 06:45:00   # 每天 06:45 喚醒（或開機）
   pmset -g sched                                       # 確認
   sudo pmset repeat cancel                             # 取消
   ```
   需要兩個時間都喚醒時，可用一次性排程（例如每週日晚上把下一週排好）：
   `sudo pmset schedule wake "10/26/26 20:25:00"`。
3. 筆電闔上螢幕時預設會睡眠；比賽期間請保持開蓋，或外接螢幕與電源（clamshell 模式）。

## 注意

- `.env`（GOOGLE_API_KEY、TEAM_ID…）放在專案根目錄；launchd 不讀 shell 的設定檔，程式會自己讀 `.env`。
- 第一次執行若 macOS 跳出「python 想要存取文件／桌面資料夾」，請允許；專案放在 `~/Documents` 以外（例如 `~/esun-etf-agent`）可避免這類權限問題。
- **資料包在 `~/Downloads`**：macOS 預設不讓 launchd 執行的 python 讀「下載項目」。第一次跳出「python 想要存取下載項目資料夾」請允許；
  沒跳出而摘要寫「無法讀取 …/Downloads」時，到「系統設定 → 隱私權與安全性 → 完整磁碟取用權限」加入 `.venv/bin/python`
  （實際檔案路徑可用 `realpath .venv/bin/python` 查），或改把資料包放到專案的 `data/datapack/`。
- 20:30 的排程只在 19:00 後已下載資料包時才會產生可上傳的 D-Plan；沒有資料包時摘要會標「不可上傳」，下載後手動重跑 `python scripts/daily_run.py`。
- 每天請打開 `runs/<交易日>/raw/summary_*.txt` 或 log 確認，**原封不動**上傳 D-Plan、按「送出繳交」、到「每日數據 → 當日委託」核對。
