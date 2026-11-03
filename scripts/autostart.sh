#!/bin/zsh
# Start `sommus live` in a Terminal window at login, so Sommus is just there instead of a chore to start.
#
# It has to run inside Terminal.app: macOS gave Terminal the microphone, Accessibility and Automation
# permissions, and a background launchd job would have none of them. So the login job only asks Terminal
# to open sommus-live.command.
#
#   scripts/autostart.sh install     # from now on, every login
#   scripts/autostart.sh uninstall   # stop doing that
set -euo pipefail

repo=${0:A:h:h}
label=com.sommus.live
plist=~/Library/LaunchAgents/$label.plist
command_file=$repo/scripts/sommus-live.command

case ${1:-} in
install)
  uv=$(command -v uv) || { echo "uv isn't on PATH"; exit 1; }
  cat >"$command_file" <<EOF
#!/bin/zsh
# Opened by Terminal at login (scripts/autostart.sh). Closing this window stops Sommus.
cd "$repo"
exec "$uv" run --quiet sommus live
EOF
  chmod +x "$command_file"
  mkdir -p ~/Library/LaunchAgents
  cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/open</string><string>-a</string><string>Terminal</string><string>$command_file</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "Installed: Sommus live opens in Terminal at every login (and just now)."
  ;;
uninstall)
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$plist" "$command_file"
  echo "Removed: Sommus no longer starts at login."
  ;;
*)
  echo "usage: scripts/autostart.sh install|uninstall"
  exit 2
  ;;
esac
