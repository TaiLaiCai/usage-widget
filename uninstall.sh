#!/bin/bash
# usage-widget 卸载脚本
set -euo pipefail

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

BIN="$HOME/.local/bin"
DATA="$HOME/.local/share/usage-monitor"
AGENTS="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"

for label in com.local.usage-widget com.local.usage-widget-data com.local.usage-wallpaper; do
  launchctl bootout "gui/$UID_NUM/$label" >/dev/null 2>&1 || true
  rm -f "$AGENTS/$label.plist"
done

for f in usage-widget usage-widget-data usage-wallpaper usage-monitor; do
  rm -f "$BIN/$f"
done

if [ "$PURGE" -eq 1 ]; then
  rm -rf "$DATA"
  echo "已卸载，并清除数据目录"
else
  echo "已卸载。数据目录保留在 $DATA（加 --purge 一起删除）"
fi
