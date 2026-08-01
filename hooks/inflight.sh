#!/usr/bin/env bash
# Tell a starting session what is already in flight on the repo it opened in.
#
# Installed and wired by `agent hooks install` (declaration: hooks/hooks.json):
#   SessionStart -> once per session, before the first prompt
#
# Why this exists, concretely. On 2026-07-29 an agent session opened PR #59
# raising the auto-compact window to 450k. On 2026-08-01 another session was
# asked for the same change, checked that its checkout was current -- it was --
# wrote the change from scratch as #63, and merged it. #59 was left permanently
# conflicted: every line it touched had been changed underneath it, so it could
# not be rebased, only abandoned.
#
# Pulling main would not have prevented that, and that is the point. The base WAS
# current. The duplicated work was never on main -- it was sitting in an open
# pull request, where git cannot see it. So this asks GitHub.
#
# Unlike its two siblings, this one writes to the MODEL's context
# (hookSpecificOutput.additionalContext), not to a systemMessage. The asymmetry
# is deliberate: the human is not the actor about to open a duplicate PR. An
# agent that cannot see #59 will write #59 again no matter what the terminal
# says. That is a real context cost, so it is bounded -- ten PRs, one short line
# each, and nothing at all when the repo is quiet.
#
# Subagents are skipped: they inherit the session's cwd, and the main session has
# already carried the block.
#
# Test seam: `inflight.sh --print < payload.json` prints the block it would
# inject (empty when nothing is in flight) and touches no state.
set -e

# Bounded because it reaches the network. A session must start whether or not
# GitHub answers.
readonly CHECK_TIMEOUT=25

# The INSTALLED CLI, never ./agent from a checkout -- a hook that depends on
# tracked files breaks exactly when the checkout is what is broken.
readonly INSTALLED_AGENT="${HOME}/.local/bin/agent"

inflight::agent() {
  if [[ -x $INSTALLED_AGENT ]]; then
    printf '%s' "$INSTALLED_AGENT"
  else
    command -v agent 2>/dev/null || true
  fi
}

inflight::is_subagent() {
  local payload="$1"
  [[ -n "$(jq -r '.agent_type // empty' <<<"$payload" 2>/dev/null || true)" ]]
}

inflight::block() {
  local payload="$1"
  local cli cwd
  cli="$(inflight::agent)"
  if [[ -z $cli ]]; then
    return 0
  fi
  # Which repo the path is in is resolved by the CLI, not here -- `agent pull
  # --here` asks the same question and there should be one answer to it.
  cwd="$(jq -r '.cwd // empty' <<<"$payload" 2>/dev/null || true)"
  if [[ -z $cwd ]]; then
    cwd="$PWD"
  fi
  # An older CLI without `inflight` prints nothing and is silently tolerated.
  timeout "$CHECK_TIMEOUT" "$cli" inflight --path "$cwd" --context 2>/dev/null || true
}

main() {
  local print_only="no"
  if [[ ${1:-} == "--print" ]]; then
    print_only="yes"
  fi

  local payload block
  payload="$(cat)"

  if [[ $print_only == "no" ]] && inflight::is_subagent "$payload"; then
    exit 0
  fi

  block="$(inflight::block "$payload")"

  if [[ $print_only == "yes" ]]; then
    printf '%s\n' "$block"
    exit 0
  fi

  if [[ -z ${block//[[:space:]]/} ]]; then
    exit 0
  fi

  jq -n --arg ctx "$block" \
    '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'
}

main "$@"
