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
# One copy only. A pid file, not pgrep: the shells that launch this file have its name in their command line.
pidfile="$repo/data/live.pid"
if [ -f "\$pidfile" ] && kill -0 "\$(cat "\$pidfile")" 2>/dev/null; then
  echo "Sommus live is already running in another window."
  exit 0
fi
echo \$\$ >"\$pidfile"
log="$repo/data/live.log"
beat="$repo/data/live.heartbeat"
# Safety net (2026-11-17): Sommus once sat deaf for 6 days with its process alive, so "restart if it exits" never
# fired. The audio code touches the heartbeat file while microphone audio flows; if it goes stale for 3 minutes
# the process is killed and the loop below starts a clean one. A gap in this watcher's own clock means the Mac
# was asleep, so that round is skipped.
(
  last=\$(date +%s)
  while true; do
    sleep 30
    now=\$(date +%s)
    gap=\$((now - last))
    last=\$now
    [ "\$gap" -gt 90 ] && continue
    age=\$((now - \$(stat -f %m "\$beat" 2>/dev/null || echo "\$now")))
    if [ "\$age" -gt 180 ]; then
      echo "No microphone heartbeat for \${age} s (\$(date +%H:%M)): restarting Sommus live."
      pkill -f ".venv/bin/sommus live"
      pkill -f "uv run --quiet --no-sync sommus live"
      touch "\$beat"
    fi
  done
) &
watchdog=\$!
trap 'rm -f "\$pidfile"; kill \$watchdog 2>/dev/null' EXIT
while true; do
  # The app follows this log; keep it small.
  if [ -f "\$log" ] && [ "\$(stat -f %z "\$log")" -gt 5000000 ]; then : >"\$log"; fi
  touch "\$beat"
  # script keeps a real terminal (colours, prompts) while copying everything to the log for the app.
  script -q -a -t 0 "\$log" "$uv" run --quiet --no-sync sommus live
  code=\$?
  if [ "\$code" -eq 75 ]; then
    # The audio engine could not bring the microphone back (lid closed with no external mic, or no input device).
    # Trying again every few seconds would only burn CPU, so wait longer.
    echo "Sommus live found no microphone audio (\$(date +%H:%M)). Trying again in 2 minutes."
    sleep 120
  else
    echo "Sommus live stopped (\$(date +%H:%M)). Starting again in 5 s; close this window to stop it."
    sleep 5
  fi
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
