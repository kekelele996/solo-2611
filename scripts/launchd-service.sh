#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
LABEL="com.gaobo.solo-auto"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="$ROOT/backend/.venv/bin/python"
LOG_DIR="${SOLO_AUTO_HOME:-$HOME/solo-auto}/logs"

if [ "${1:-}" = "uninstall" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
  rm -f "$PLIST"
  printf '已卸载 %s\n' "$LABEL"
  exit 0
fi

if [ "${1:-}" != "install" ]; then
  echo "用法: $0 install|uninstall" >&2
  exit 2
fi
if [ ! -x "$PYTHON" ]; then
  echo "缺少 $PYTHON，请先运行 make setup" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>solo_auto</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SOLO_AUTO_HOME</key><string>${SOLO_AUTO_HOME:-$HOME/solo-auto}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOG_DIR/launchd.stdout.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/launchd.stderr.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"
printf '已安装并启动 %s；仅监听配置中的 127.0.0.1 地址\n' "$LABEL"
