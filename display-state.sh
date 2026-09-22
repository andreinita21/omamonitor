#!/bin/bash
# Prints what the Display panel needs in two lines:
#   1. `hyprctl monitors all -j`, compacted to one line
#   2. JSON with the lid state, whether Omarchy's clamshell toggle is holding
#      the laptop panel off, and which outputs monitors.lua marks disabled
#      (those were switched off in the panel on purpose, so auto-enable
#      leaves them alone).

hyprctl monitors all -j | jq -c .

lid=unknown
for f in /proc/acpi/button/lid/*/state; do
  [[ -r $f ]] || continue
  if grep -q closed "$f"; then lid=closed; else lid=open; fi
  break
done

clamshell=false
[[ -f "$HOME/.local/state/omarchy/toggles/hypr/internal-monitor-clamshell.lua" ]] && clamshell=true

monitors_lua="${XDG_CONFIG_HOME:-$HOME/.config}/hypr/monitors.lua"
disabled='[]'
if [[ -f $monitors_lua ]]; then
  disabled=$(sed -E 's/--.*$//' "$monitors_lua" \
    | grep -oE 'output[[:space:]]*=[[:space:]]*"[A-Za-z0-9._-]+"[^}]*disabled[[:space:]]*=[[:space:]]*true' \
    | sed -E 's/^output[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/' \
    | jq -R . | jq -sc .)
  [[ -n $disabled ]] || disabled='[]'
fi

jq -nc --arg lid "$lid" --argjson clamshell "$clamshell" --argjson disabled "$disabled" \
  '{lid: $lid, clamshell: $clamshell, userDisabled: $disabled}'
