-- Double-click to open the Sommus app window. It turns Sommus on first if it's off: the 24/7 `sommus live`
-- window (voice, the Mac's tools) is started in Terminal, then the small local app server, then the window.
-- Build: osacompile -o ~/Applications/Sommus.app scripts/Sommus.applescript
set repo to "/Users/jashandeepsinghmultani/Documents/03_Technical_and_Projects/Active_Repos/sommus"
do shell script "cd " & quoted form of repo & " && (([ -f data/live.pid ] && kill -0 $(cat data/live.pid) 2>/dev/null) || ([ -x scripts/sommus-live.command ] && open -a Terminal scripts/sommus-live.command)); (curl -s -m 1 http://127.0.0.1:8791/api/status >/dev/null && open -na 'Google Chrome' --args --app=http://127.0.0.1:8791 --window-size=1100,760 || nohup /opt/homebrew/bin/uv run --no-sync sommus ui >/tmp/sommus-ui.log 2>&1 &)"
