#!/bin/bash
# Emits the omarchy-agent-session list --json payload unchanged on success,
# or {"error": "<reason>"} on failure. Only stdout JSON matters; everything
# else (the underlying command's own stderr) passes through to our stderr,
# same convention as the Hermes reference script.
#
# Bounded by design: output over 64 KB is treated as a failure, not
# truncated, so Panel.qml's own 64 KB parser cap is never the only seatbelt.
#
# For local testing, point OMARCHY_SESSION_LIST_CMD at a stand-in command
# (its value is run through `bash -c`, so it may be a full pipeline):
#
#   OMARCHY_SESSION_LIST_CMD="cat fixture.json" ./snapshot.sh
#
# VERIFY ON RIG: the real binary name for "list --json" is unconfirmed.
# 02-command-surface.md states the convention bin/omarchy-agent-session-<cmd>
# (which would make this omarchy-agent-session-list) but its own worked
# examples call the grouped CLI form `omarchy agent session list --json`;
# 03-sessions-panel.md's illustrative Process block uses yet a third form,
# a bare `omarchy-agent-session list --json` dispatcher. This script tries
# omarchy-agent-session-core first because that is the literal primary this
# skeleton was asked to use, then falls back to omarchy-agent-session-list.
# None of the three sources agree, so treat all of this as unverified.

set -o pipefail

MAX_BYTES=65536

run_list() {
  if [[ -n "${OMARCHY_SESSION_LIST_CMD:-}" ]]; then
    bash -c "$OMARCHY_SESSION_LIST_CMD"
    return $?
  fi

  if command -v omarchy-agent-session-core >/dev/null 2>&1; then
    omarchy-agent-session-core list --json
    return $?
  fi

  if command -v omarchy-agent-session-list >/dev/null 2>&1; then
    omarchy-agent-session-list --json
    return $?
  fi

  return 127
}

error_json() {
  local reason=$1
  # Minimal JSON string escaping (backslash, double-quote): the only
  # characters any reason string built below can ever contain.
  reason=${reason//\\/\\\\}
  reason=${reason//\"/\\\"}
  printf '{"error": "%s"}\n' "$reason"
}

raw=$(run_list)
status=$?

if [[ $status -ne 0 ]]; then
  if [[ $status -eq 127 ]]; then
    error_json "no session list command found"
  else
    error_json "session list command failed (exit $status)"
  fi
  exit 0
fi

if [[ -z "$raw" ]]; then
  error_json "session list command produced no output"
  exit 0
fi

byte_len=$(printf '%s' "$raw" | wc -c | tr -d ' ')
if (( byte_len > MAX_BYTES )); then
  error_json "session list output exceeded 64 KB"
  exit 0
fi

if ! printf '%s' "$raw" | python3 -c 'import json, sys; json.load(sys.stdin)' >/dev/null 2>&1; then
  error_json "session list output was not valid JSON"
  exit 0
fi

printf '%s\n' "$raw"
