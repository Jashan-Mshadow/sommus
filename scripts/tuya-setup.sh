#!/bin/zsh
# Put the Tuya (Merkury) Access ID, Access Secret and region into .env here and on the server, then restart the brain.
# Nothing is echoed. Find the keys on iot.tuya.com > your cloud project > Overview.
set -euo pipefail
repo=${0:A:h:h}
read -r "id?Tuya Access ID: "
read -rs "secret?Tuya Access Secret: "; echo
read -r "region?Region (us / us-east / eu / cn / in) [us]: "
region=${region:-us}
[[ -n $id && -n $secret ]] || { echo "Both are needed."; exit 1; }

set_keys() {  # $1 = path of a .env file; drops old lines, appends the new ones
  local file=$1
  touch "$file"
  grep -v '^TUYA_\(ACCESS_ID\|ACCESS_SECRET\|REGION\)=' "$file" >"$file.tmp" || true
  printf 'TUYA_ACCESS_ID=%s\nTUYA_ACCESS_SECRET=%s\nTUYA_REGION=%s\n' "$id" "$secret" "$region" >>"$file.tmp"
  mv "$file.tmp" "$file" && chmod 600 "$file"
}
set_keys "$repo/.env"
printf '%s\n%s\n%s\n' "$id" "$secret" "$region" | ssh sommus-brain 'read -r i; read -r s; read -r r; f=~/sommus/.env
  grep -v "^TUYA_\(ACCESS_ID\|ACCESS_SECRET\|REGION\)=" "$f" >"$f.tmp" || true
  printf "TUYA_ACCESS_ID=%s\nTUYA_ACCESS_SECRET=%s\nTUYA_REGION=%s\n" "$i" "$s" "$r" >>"$f.tmp"; mv "$f.tmp" "$f"; chmod 600 "$f"
  sudo -n systemctl restart sommus'
echo "Saved on the Mac and the server; the brain is restarting. Try: \"turn on the lamp\"."
