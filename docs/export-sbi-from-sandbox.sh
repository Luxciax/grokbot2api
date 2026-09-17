#!/usr/bin/env bash
# Run INSIDE the Grok Bot remote sand box (Computer → Linux terminal).
# Finds SAND_INFERENCE_RENEWAL_CREDENTIAL (sbi_...) and writes a local JSON export.
# Do not commit the JSON. Do not paste the secret into chat / git / issues.

set -euo pipefail

VAR_NAME="SAND_INFERENCE_RENEWAL_CREDENTIAL"
OUT_NAME="grokbot2api-sbi-export.json"

is_sbi() {
  local v="$1"
  [[ "$v" == sbi_* && ${#v} -ge 20 && ${#v} -le 80 ]]
}

found=""
if [[ -n "${!VAR_NAME:-}" ]]; then
  found="${!VAR_NAME}"
  echo "source=current_shell"
fi

if [[ -z "$found" && -d /proc ]]; then
  for envf in /proc/[0-9]*/environ; do
    [[ -r "$envf" ]] || continue
    val="$(tr '\0' '\n' < "$envf" 2>/dev/null | sed -n "s/^${VAR_NAME}=//p" | head -n1 || true)"
    if is_sbi "${val:-}"; then
      found="$val"
      echo "source=${envf%%/environ}"
      break
    fi
  done
fi

if ! is_sbi "${found:-}"; then
  echo "STATUS=not_found"
  echo "Run: printenv ${VAR_NAME}"
  echo "If still empty, this terminal is not the process that received sbi_."
  exit 2
fi

echo "STATUS=ok"
echo "PREFIX=${found:0:4}"
echo "LENGTH=${#found}"

out="/tmp/$OUT_NAME"
for d in /mnt/c/Users/V/grokbot2api /host/Users/V/grokbot2api "${HOME}/grokbot2api" /tmp; do
  if [[ -d "$d" ]]; then out="$d/$OUT_NAME"; break; fi
done

umask 077
# Minimal JSON string escape
esc=$(printf '%s' "$found" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read())[1:-1])')
printf '{\n  "source": "sand-box-env",\n  "var": "%s",\n  "value": "%s"\n}\n' "$VAR_NAME" "$esc" > "$out"
chmod 600 "$out" 2>/dev/null || true
echo "WROTE=$out"
echo "NEXT=Copy value into grokbot2api → 凭证; then delete this JSON file."
