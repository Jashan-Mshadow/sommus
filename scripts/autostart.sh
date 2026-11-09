#!/bin/zsh
# Start `sommus live` in a Terminal window at login and keep it running: it's the one Mac mode (voice and wake
# word, the Mac's tools for the always-on brain, the Sommus app's feed), meant to be on 24/7 (Jashan, 2026-11-09).
#
# It has to run inside Terminal.app: macOS gave Terminal the microphone, Accessibility and Automation
# permissions, and a background launchd job would have none of them. So the login job only asks Terminal
# to open sommus-live.command, which restarts Sommus live whenever it stops.
#
#   scripts/autostart.sh install     # from now on, every login (and right now)
#   scripts/autostart.sh uninstall   # stop doing that
set -euo pipefail

repo=${0:A:h:h}
label=com.sommus.live
plist=~/Library/LaunchAgents/$label.plist
command_file=$repo/scripts/sommus-live.command

case ${1:-} in
install)
  uv=$(command -v uv) || { echo "uv isn't on PATH"; exit 1; }
  mkdir -p "$repo/data"
  cat >"$command_file" <<END_COMMAND
#!/bin/zsh
# Sommus live, the one Mac mode, kept on 24/7. Opened by Terminal at login (scripts/autostart.sh) and by the
# Sommus app when it's off. If it stops or crashes it starts again in 5 s. Closing this window stops it.
cd "$repo"
others=\$(pgrep -f "sommus-live.command" | grep -vx "\$\$" | wc -l | tr -d ' ')
if pgrep -f "bin/sommus live" >/dev/null || [ "\$others" -gt 0 ]; then
  echo "Sommus live is already running in another window."
  exit 0
fi
log="$repo/data/live.log"
while true; do
  # The app follows this log; keep it small.
  if [ -f "\$log" ] && [ "\$(stat -f %z "\$log")" -gt 5000000 ]; then : >"\$log"; fi
  # script keeps a real terminal (colours, prompts) while copying everything to the log for the app.
  script -q -a -t 0 "\$log" "$uv" run --quiet --no-sync sommus live
  echo "Sommus live stopped (\$(date +%H:%M)). Starting again in 5 s; close this window to stop it."
  sleep 5
done
END_COMMAND
  chmod +x "$command_file"
  mkdir -p ~/Library/LaunchAgents
  cat >"$plist" <<END_PLIST
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
END_PLIST
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "Installed: Sommus live opens in Terminal at every login (and just now), and restarts if it stops."
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
