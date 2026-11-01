#!/bin/bash
# usage-widget 安装脚本
set -euo pipefail

VERSION="1.0.0"
REFRESH=3600
DRY_RUN=0
SKIP_AGENTS=0
LABEL_WIDGET="com.local.usage-widget"
LABEL_DATA="com.local.usage-widget-data"

usage() {
  cat <<'EOF'
用法: ./install.sh [选项]

  --refresh-seconds N   数据刷新间隔，默认 3600（1 小时）
  --no-agents           只装文件，不注册 LaunchAgent
  --dry-run             只打印动作，不实际执行
  -h, --help            显示帮助
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --refresh-seconds) REFRESH="$2"; shift 2 ;;
    --no-agents)       SKIP_AGENTS=1; shift ;;
    --dry-run)         DRY_RUN=1; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

SRC="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
DATA="$HOME/.local/share/usage-monitor"
AGENTS="$HOME/Library/LaunchAgents"

say() { echo "  $*"; }
run() { if [ "$DRY_RUN" -eq 1 ]; then echo "  [dry] $*"; else "$@"; fi }

[ "$(uname -s)" = "Darwin" ] || { echo "仅支持 macOS" >&2; exit 1; }

echo "usage-widget $VERSION"
echo "  安装到 $BIN"
echo "  数据目录 $DATA"
echo ""

# 组件二进制：优先用预编译产物，其次本地构建（git clone 场景）
if [ -x "$SRC/bin/usage-widget" ]; then
  WIDGET_SRC="$SRC/bin/usage-widget"
elif [ -x "$SRC/scripts/build.sh" ] && command -v swiftc >/dev/null 2>&1; then
  say "未找到预编译组件，使用本地 swiftc 构建（约 30 秒）"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "[dry] $SRC/scripts/build.sh"
    WIDGET_SRC="$SRC/bin/usage-widget"
  else
    "$SRC/scripts/build.sh" >/dev/null
    WIDGET_SRC="$SRC/bin/usage-widget"
  fi
else
  echo "缺少组件二进制，且没有 swiftc。请先安装命令行工具：" >&2
  echo "  xcode-select --install" >&2
  exit 1
fi

# 其余脚本：发布包里在 bin/，源码仓库里在 src/ 与 scripts/
pick() {
  local name="$1"
  for cand in "$SRC/bin/$name" "$SRC/src/$name" "$SRC/scripts/$name" "$SRC/src/$name.py"; do
    [ -f "$cand" ] && { echo "$cand"; return 0; }
  done
  return 1
}

MON_SRC="$(pick usage-monitor)"       || { echo "找不到 usage-monitor" >&2; exit 1; }
DATA_SRC="$(pick usage-widget-data)"  || { echo "找不到 usage-widget-data" >&2; exit 1; }
WALL_SRC="$(pick usage-wallpaper)"    || true

run mkdir -p "$BIN" "$DATA" "$AGENTS"

run cp -f "$MON_SRC" "$BIN/usage-monitor";  run chmod 755 "$BIN/usage-monitor"
say "已安装 usage-monitor"

run cp -f "$DATA_SRC" "$BIN/usage-widget-data"; run chmod 755 "$BIN/usage-widget-data"
say "已安装 usage-widget-data"

if [ -n "${WALL_SRC:-}" ]; then
  run cp -f "$WALL_SRC" "$BIN/usage-wallpaper"; run chmod 755 "$BIN/usage-wallpaper"
  say "已安装 usage-wallpaper（可选）"
fi

run cp -f "$WIDGET_SRC" "$BIN/usage-widget"
run chmod 755 "$BIN/usage-widget"
run codesign --force --sign - "$BIN/usage-widget" >/dev/null 2>&1 || true
say "已安装 usage-widget"

if [ "$DRY_RUN" -eq 0 ]; then
  if "$BIN/usage-monitor" --json --days 7 > "$DATA/widget-data.json.tmp" 2>/dev/null; then
    mv -f "$DATA/widget-data.json.tmp" "$DATA/widget-data.json"
    say "已生成首份数据快照"
  else
    rm -f "$DATA/widget-data.json.tmp"
    say "首次取数失败（未登录相关 CLI 时正常），后台任务会定期重试"
  fi
fi

if [ "$SKIP_AGENTS" -eq 1 ]; then
  echo ""
  say "已跳过 LaunchAgent。手动启动：$BIN/usage-widget &"
  exit 0
fi

# 笔记本睡眠时 StartInterval 不推进（只在清醒时间累积），
# 所以整小时的间隔一律用 StartCalendarInterval —— 它有「唤醒后补跑」语义。
schedule_block() {
  local secs="$1"
  if [ $((secs % 3600)) -eq 0 ]; then
    local step=$((secs / 3600))
    if [ "$step" -eq 1 ]; then
      printf '    <key>StartCalendarInterval</key>\n    <dict>\n        <key>Minute</key>\n        <integer>0</integer>\n    </dict>'
      return
    fi
    if [ "$step" -le 12 ]; then
      local hours="" h
      for ((h = 0; h < 24; h += step)); do hours="$hours<integer>$h</integer>"; done
      printf '    <key>StartCalendarInterval</key>\n    <dict>\n        <key>Minute</key>\n        <integer>0</integer>\n        <key>Hour</key>\n        <array>%s</array>\n    </dict>' "$hours"
      return
    fi
  fi
  printf '    <key>StartInterval</key>\n    <integer>%s</integer>' "$secs"
}

write_plist() {
  local path="$1" label="$2" interval="$3"
  local schedule program
  if [ -n "$interval" ]; then
    schedule="    <key>RunAtLoad</key>
    <true/>
$(schedule_block "$interval")"
  else
    schedule="    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>"
  fi

  if [ "$label" = "$LABEL_WIDGET" ]; then
    program="        <string>$BIN/usage-widget</string>"
  else
    program="        <string>/bin/bash</string>
        <string>$BIN/usage-widget-data</string>"
  fi

  cat > "$path" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
$program
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
        <key>PATH</key>
        <string>$BIN:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
$schedule
    <key>StandardOutPath</key>
    <string>$DATA/widget.log</string>
    <key>StandardErrorPath</key>
    <string>$DATA/widget.log</string>
</dict>
</plist>
PLIST
}

WIDGET_PLIST="$AGENTS/$LABEL_WIDGET.plist"
DATA_PLIST="$AGENTS/$LABEL_DATA.plist"

if [ "$DRY_RUN" -eq 0 ]; then
  write_plist "$WIDGET_PLIST" "$LABEL_WIDGET" ""
  write_plist "$DATA_PLIST" "$LABEL_DATA" "$REFRESH"
  say "已写入两个 LaunchAgent"
else
  say "[dry] 写入 $WIDGET_PLIST 与 $DATA_PLIST（间隔 ${REFRESH}s）"
fi

UID_NUM="$(id -u)"
for label in "$LABEL_DATA" "$LABEL_WIDGET"; do
  run launchctl bootout "gui/$UID_NUM/$label" >/dev/null 2>&1 || true
done
run sleep 1
for label in "$LABEL_DATA" "$LABEL_WIDGET"; do
  run launchctl bootstrap "gui/$UID_NUM" "$AGENTS/$label.plist"
done

echo ""
echo "完成。"
echo "  看状态:   launchctl print gui/$UID_NUM/$LABEL_WIDGET | head"
echo "  立即刷新: launchctl kickstart -k gui/$UID_NUM/$LABEL_DATA"
echo "  卸载:     ./uninstall.sh"
