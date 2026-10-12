#!/bin/bash
# Prints what the Display panel needs in two lines:
#   1. `hyprctl monitors all -j`, compacted to one line
#   2. JSON with the lid state, whether Omarchy's clamshell toggle is holding
#      the laptop panel off, and the rules monitors.lua has saved (parsed by
#      write-monitors.py --dump). Rules marked `disabled` were switched off in
#      the panel on purpose, so auto-enable leaves those displays alone; the
#      positions let a display that comes back return to where it was.

dir=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")

hyprctl monitors all -j | jq -c .

lid=unknown
for f in /proc/acpi/button/lid/*/state; do
  [[ -r $f ]] || continue
  if grep -q closed "$f"; then lid=closed; else lid=open; fi
  break
done

clamshell=false
[[ -f "$HOME/.local/state/omarchy/toggles/hypr/internal-monitor-clamshell.lua" ]] && clamshell=true

rules=$(python3 "$dir/write-monitors.py" --dump 2>/dev/null)
[[ $rules == \[* ]] || rules='[]'

jq -nc --arg lid "$lid" --argjson clamshell "$clamshell" --argjson rules "$rules" \
  '{lid: $lid, clamshell: $clamshell, rules: $rules}'
