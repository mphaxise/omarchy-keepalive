#!/bin/bash
# Keepalive for Omarchy: link the commands into ~/.local/bin and install
# the two user units. Run after `omarchy plugin add`, and again after
# `omarchy plugin update`. Nothing here needs root, downloads anything, or
# replaces a file this script did not create.
#
#   ~/.config/omarchy/plugins/<id>/install.sh
#
# Environment: OMARCHY_AGENT_SESSIONS_BINDIR (default ~/.local/bin).

set -euo pipefail

here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
bindir=${OMARCHY_AGENT_SESSIONS_BINDIR:-$HOME/.local/bin}
unitdir=$HOME/.config/systemd/user
units=(omarchy-agent-session-herdr.service omarchy-agent-session-watch.service)

say() { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

plugin_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$here/manifest.json" 2>/dev/null) \
  || fail "cannot read $here/manifest.json"

# ---- requirements --------------------------------------------------------

command -v python3 >/dev/null || fail "python3 is required (the core is Python, standard library only)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || fail "python3 3.11 or later is required"
command -v herdr >/dev/null || fail "herdr is required; Omarchy Quattro installs it"

missing=()
for c in hyprctl grim omarchy-launch-or-focus-tui omarchy-notification-send omarchy-shell; do
  command -v "$c" >/dev/null || missing+=("$c")
done
if (( ${#missing[@]} )); then
  say "warning: not on PATH here: ${missing[*]} (captures, preview focus, terminals, or notifications will be limited)"
fi

# ---- commands ------------------------------------------------------------

mkdir -p "$bindir"
linked=0
for src in "$here"/bin/omarchy-agent-session*; do
  [[ -f $src ]] || continue
  name=$(basename "$src")
  dst="$bindir/$name"
  if [[ -L $dst ]]; then
    target=$(readlink -f "$dst" || true)
    if [[ $target != "$here"/* ]]; then
      fail "$dst is a symlink to $target, which is not this plugin; remove it yourself first"
    fi
  elif [[ -e $dst ]]; then
    fail "$dst already exists and was not created by this plugin; move it aside first"
  fi
  chmod +x "$src"
  ln -sfn "$src" "$dst"
  linked=$((linked + 1))
done
chmod +x "$here"/scripts/* 2>/dev/null || true
say "linked $linked commands into $bindir"

case ":$PATH:" in
  *":$bindir:"*) ;;
  *) say "note: $bindir is not on this shell's PATH; Omarchy's session PATH includes ~/.local/bin, so the bar widget and the units will still find the commands" ;;
esac

# ---- user units ----------------------------------------------------------

mkdir -p "$unitdir"
for unit in "${units[@]}"; do
  src="$here/systemd/$unit"
  dst="$unitdir/$unit"
  [[ -f $src ]] || fail "missing $src"
  if [[ -e $dst ]] && ! cmp -s "$src" "$dst"; then
    if ! grep -q 'omarchy-multiplayer' "$dst"; then
      fail "$dst exists and is not this plugin's unit; move it aside first"
    fi
  fi
  install -m 0644 "$src" "$dst"
done
systemctl --user daemon-reload
systemctl --user enable --now "${units[@]}"
say "enabled ${units[*]}"

# ---- what is left to you -------------------------------------------------

cat <<EOF

Done. The Keepalive widget is enabled by 'omarchy plugin enable $plugin_id' (or --enable on add).

Optional keybindings, yours to add to ~/.config/hypr/bindings.lua:

  o.bind("SUPER + CTRL + G", "Keepalive", "omarchy-shell $plugin_id toggle")
  o.bind("SUPER + CTRL + SHIFT + G", "Agent that needs you", "omarchy-shell $plugin_id openMostUrgent")

First session:

  omarchy-agent-session-new --agent claude --mode personal --goal "..."
EOF
