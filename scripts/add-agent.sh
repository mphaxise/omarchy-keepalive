#!/bin/bash
# Add an agent to a live session from the panel (11-agent-lanes.md).
#
#   add-agent.sh <session-id> "<kind> <task>"     the first word names a harness
#   add-agent.sh <session-id> "<task>"            the default agent, the whole text as the task
#
# Exit codes: 0 added; 6 no agent named and no default agent; otherwise
# omarchy-agent-session-add's own code (3 not found, 4 Herdr unreachable,
# 5 the session's state or the lane rules forbid it).
set -uo pipefail

sid=${1:?session id}
text=${2:-}
kinds=" claude codex opencode copilot crush grok hermes agy omp pi ori "

first=${text%% *}
rest=${text#* }
[[ $rest == "$text" ]] && rest=""
if [[ -n $first && $kinds == *" ${first,,} "* ]]; then
  kind=${first,,}
  task=$rest
else
  kind=$(omarchy-default-agent 2>/dev/null || true)
  task=$text
  if [[ -z $kind ]]; then
    echo "name the agent first (claude, codex, …) or choose a default with: omarchy default agent <name>" >&2
    exit 6
  fi
fi

args=(--agent "$kind")
[[ -n $task ]] && args+=(--task "$task")
exec omarchy-agent-session-add "$sid" "${args[@]}"
