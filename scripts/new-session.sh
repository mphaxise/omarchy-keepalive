#!/bin/bash
# Start a session from the panel: the default agent, the panel's directory,
# the typed text as the first prompt, then a terminal on it.
#
#   new-session.sh "<prompt, or empty>" [dir] [mode]
#
# Exit codes: 0 started; 6 no default agent; 7 the directory does not
# exist; otherwise the exit code of omarchy-agent-session-new or -open
# (3 not found, 4 Herdr unreachable, 5 the state forbids it).
set -uo pipefail

text=${1:-}
dir=${2:-${OMARCHY_AGENT_SESSIONS_NEW_DIR:-$HOME/Work}}
mode=${3:-personal}
dir=${dir/#\~/$HOME}

agent=$(omarchy-default-agent 2>/dev/null || true)
if [[ -z $agent ]]; then
  echo "no default agent; choose one with: omarchy default agent <name>" >&2
  exit 6
fi
if [[ ! -d $dir ]]; then
  echo "directory not found: $dir" >&2
  exit 7
fi

args=(--agent "$agent" --mode "$mode" --cwd "$dir")
[[ -n $text ]] && args+=(--prompt "$text")

id=$(omarchy-agent-session-new "${args[@]}") || exit $?
omarchy-agent-session-open "$id" || exit $?
echo "$id"
