#!/bin/bash
# 安裝 launchd 排程：晚上 20:30（週日～週四，即每個交易日的前一天）跑 daily_run、清晨 06:50（週一～週五）跑 --morning。
#   bash scripts/launchd/install.sh                 # 使用 ./.venv/bin/python（沒有就用 python3）
#   PYTHON=/path/to/python bash scripts/launchd/install.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$REPO/.venv/bin/python" ]; then PY="$REPO/.venv/bin/python"; else PY="$(command -v python3)"; fi
fi
DEST="$HOME/Library/LaunchAgents"
mkdir -p "$DEST" "$REPO/runs/launchd"
for name in evening morning; do
  label="tw.esun.dplan.$name"
  sed -e "s#__REPO__#$REPO#g" -e "s#__PYTHON__#$PY#g" "$REPO/scripts/launchd/$label.plist" > "$DEST/$label.plist"
  plutil -lint "$DEST/$label.plist" >/dev/null
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$DEST/$label.plist"
  echo "已安裝 $label → $DEST/$label.plist"
done
echo "Python：$PY"
echo "確認：launchctl print gui/$(id -u)/tw.esun.dplan.evening | grep -E 'state|next'"
echo "立即試跑：launchctl kickstart -k gui/$(id -u)/tw.esun.dplan.evening"
echo "記得設定定時喚醒（見 scripts/launchd/README.md 的 pmset 說明）"
