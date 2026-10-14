#!/bin/bash
# 移除 launchd 排程（不會刪除 runs/ 下的任何資料）。
set -euo pipefail
for name in evening morning; do
  label="tw.esun.dplan.$name"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null && echo "已停止 $label" || echo "$label 未載入"
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
echo "如有設定 pmset 定時喚醒，請另外執行：sudo pmset repeat cancel"
