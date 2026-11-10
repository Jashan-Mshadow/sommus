#!/bin/zsh
# Put the SwitchBot token and secret into .env here and on the server, then restart the brain. Nothing is echoed.
# Find them in the SwitchBot app: Profile > Preferences > tap App Version about ten times > Developer Options.
set -euo pipefail
repo=${0:A:h:h}
read -rs "token?SwitchBot token: "; echo
read -rs "secret?SwitchBot secret key: "; echo
[[ -n $token && -n $secret ]] || { echo "Both are needed."; exit 1; }

set_keys() {  # $1 = path of a .env file; drops old lines, appends the new ones
  local file=$1
  touch "$file"
  grep -v '^SWITCHBOT_\(TOKEN\|SECRET\)=' "$file" >"$file.tmp" || true
  printf 'SWITCHBOT_TOKEN=%s\nSWITCHBOT_SECRET=%s\n' "$token" "$secret" >>"$file.tmp"
  mv "$file.tmp" "$file" && chmod 600 "$file"
}
set_keys "$repo/.env"
printf '%s\n%s\n' "$token" "$secret" | ssh sommus-brain 'read -r t; read -r s; f=~/sommus/.env
  grep -v "^SWITCHBOT_\(TOKEN\|SECRET\)=" "$f" >"$f.tmp" || true
  printf "SWITCHBOT_TOKEN=%s\nSWITCHBOT_SECRET=%s\n" "$t" "$s" >>"$f.tmp"; mv "$f.tmp" "$f"; chmod 600 "$f"
  sudo -n systemctl restart sommus'
echo "Saved on the Mac and the server; the brain is restarting. Try: \"turn on the fan\"."
