#!/usr/bin/env bash
# Pull the repo this session is working in, before it starts reading.
#
# Installed and wired by `agent hooks install` (declaration: hooks/hooks.json):
#   SessionStart -> once per session, before the first prompt
#
# Why this exists. Staying current has two halves and only one was covered.
# `agent workspace create` fetches and fast-forwards the default branch before it
# cuts a worktree, so *implementation* always starts from a fresh
# origin/<default>. *Exploration* does not: a session that opens ~/src/<repo> and
# starts grepping works against whatever was last on disk, which may be days old.
# Reading a stale tree is worse than reading no tree, because the answers look
# right.
#
# `agent pull --here` is the whole implementation; this script only decides when
# to ask and how to say it. That command fetches one checkout and fast-forwards
# it, and it declines -- saying so -- on a dirty tree or a feature branch. It is
# silent when there was nothing to do, which is the common case.
#
# Output is a `systemMessage`: the human sees that the tree moved under them,
# and the model's context pays nothing. SessionStart stdout would be injected
# into every session.
#
# Worktrees are excluded by construction: they live under ~/worktrees/, outside
# the src root this resolves against, so a feature branch with work on it is
# never a candidate.
#
# Test seam: `repo-freshness.sh --print < payload.json` prints what it would emit
# (empty when there was nothing to do).
set -e

# Fetch plus a fast-forward on one repo. Generous enough for a slow link, hard
# enough that a wedged remote cannot hold a session at the door.
readonly SYNC_TIMEOUT=20

# The INSTALLED CLI, never ./agent from a checkout -- a hook that depends on
# tracked files breaks exactly when the checkout is what is broken.
readonly INSTALLED_AGENT="${HOME}/.local/bin/agent"

repo::agent() {
  if [[ -x $INSTALLED_AGENT ]]; then
    printf '%s' "$INSTALLED_AGENT"
  else
    command -v agent 2>/dev/null || true
  fi
}

# Subagents inherit the session's cwd and would each re-ask the same question of
# the same repo. One pull per session is enough.
repo::is_subagent() {
  local payload="$1"
  [[ -n "$(jq -r '.agent_type // empty' <<<"$payload" 2>/dev/null || true)" ]]
}

repo::message() {
  local payload="$1"
  local cli cwd
  cli="$(repo::agent)"
  if [[ -z $cli ]]; then
    return 0
  fi
  # The payload's cwd is the session's, which is the one that matters; $PWD is
  # only a fallback for a hand-run or an older payload shape.
  cwd="$(jq -r '.cwd // empty' <<<"$payload" 2>/dev/null || true)"
  if [[ -z $cwd ]]; then
    cwd="$PWD"
  fi
  # An older CLI without `--here` prints nothing and is silently tolerated: the
  # right failure mode for a hook on the session's critical path.
  timeout "$SYNC_TIMEOUT" "$cli" pull --here --path "$cwd" 2>/dev/null || true
}

main() {
  local print_only="no"
  if [[ ${1:-} == "--print" ]]; then
    print_only="yes"
  fi

  local payload message
  payload="$(cat)"

  if [[ $print_only == "no" ]] && repo::is_subagent "$payload"; then
    exit 0
  fi

  message="$(repo::message "$payload")"

  if [[ $print_only == "yes" ]]; then
    printf '%s\n' "$message"
    exit 0
  fi

  if [[ -z ${message//[[:space:]]/} ]]; then
    exit 0
  fi

  jq -n --arg msg "$message" '{systemMessage: $msg}'
}

main "$@"
