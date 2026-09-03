#!/bin/bash
# Keepalive for Omarchy: undo install.sh. Stops and removes the two user
# units and the command links that point into this plugin, and leaves your
# session records alone. Then remove the plugin itself:
#
#   ~/.config/omarchy/plugins/<id>/uninstall.sh
#   omarchy plugin remove <id>
#
# Environment: OMARCHY_AGENT_SESSIONS_BINDIR (default ~/.local/bin).

set -euo pipefail

here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
bindir=${OMARCHY_AGENT_SESSIONS_BINDIR:-$HOME/.local/bin}
unitdir=$HOME/.config/systemd/user
units=(omarchy-agent-session-watch.service omarchy-agent-session-herdr.service)

say() { printf '%s\n' "$*"; }

plugin_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$here/manifest.json" 2>/dev/null || echo "<id>")

# ---- units ---------------------------------------------------------------

for unit in "${units[@]}"; do
  dst="$unitdir/$unit"
  [[ -e $dst ]] || continue
  if ! grep -q 'omarchy-multiplayer' "$dst"; then
    say "leaving $dst in place: it is not this plugin's unit"
    continue
  fi
  systemctl --user disable --now "$unit" >/dev/null 2>&1 || true
  rm -f "$dst"
  if [[ -d $dst.d ]]; then
    # A drop-in written for this unit (the rig's env.conf, for example);
    # remove it only when it sets nothing but this plugin's own variables.
    if ! grep -vhE '^\[Service\]$|^Environment=(HERDR_SOCKET|OMARCHY_SESSIONS_DIR)=|^\s*$' "$dst.d"/*.conf 2>/dev/null | grep -q .; then
      rm -rf "$dst.d"
    else
      say "leaving $dst.d in place: it holds settings that are not this plugin's"
    fi
  fi
  say "removed $unit"
done
systemctl --user daemon-reload

# ---- command links -------------------------------------------------------

removed=0
for dst in "$bindir"/omarchy-agent-session*; do
  [[ -L $dst ]] || continue
  target=$(readlink -f "$dst" || true)
  [[ $target == "$here"/* ]] || continue
  rm -f "$dst"
  removed=$((removed + 1))
done
say "removed $removed command links from $bindir"

cat <<EOF

Left in place, on purpose:
  ~/.local/state/omarchy/sessions/   your session records and receipts; delete it yourself if you want them gone
  Herdr worktrees and session/* branches in your repositories
  any keybindings you added to ~/.config/hypr/bindings.lua
  the Herdr server, if it is still running sessions

Now remove the plugin:  omarchy plugin remove $plugin_id
EOF
