#!/usr/bin/env bash
# Collect spent session worktrees at session start.
#
# Installed and wired by `agent hooks install` (declaration: hooks/hooks.json):
#   SessionStart -> startup, resume, and /clear
#
# Why here, rather than a thing to remember. A worktree is spent the moment its
# PR merges -- the branch can take no further work, because pushing to it does
# not reopen a merged PR. Nobody deletes it at that moment: the session that
# would have is the same session that just finished and moved on. So they
# accumulate, and a host with a dozen stale worktrees makes `agent workspace
# list` useless for its actual job, which is telling you what is still live.
#
# /clear is the right trigger because it is exactly the task boundary: the moment
# a session stops being about the thing it was about. Startup and resume get it
# too, since a host left alone overnight has the same pile waiting.
#
# `agent workspace gc` is the whole implementation. It removes a worktree only
# when its PR has merged or it has been idle past the window, never forces (git
# itself refuses to remove a worktree with uncommitted changes, and that refusal
# is the protection), and leaves branches intact so committed work stays
# reachable. Nothing here re-implements any of that.
#
# Output is a `systemMessage` -- the human, never the model's context. What was
# deleted is the human's business; the model does not need it and should not pay
# for it.
#
# Opt out for a session with AGENT_WORKTREE_GC=0.
#
# Test seam: `worktree-gc.sh --print < payload.json` prints the message it would
# emit and, unlike the real path, still runs the collection -- so use it against
# a scratch HOME, not a live one.
set -e

# Generous: one API call per repo that has worktrees. Short enough that a hung
# network cannot hold a session open -- a missed collection costs nothing, since
# the next session start does it again.
readonly GC_TIMEOUT=25

# The INSTALLED CLI, never ./agent from a checkout -- same rule as the credential
# helper: a hook that depends on tracked files breaks exactly when the checkout
# is the thing that is broken.
readonly INSTALLED_AGENT="${HOME}/.local/bin/agent"

gc::agent() {
  if [[ -x ${INSTALLED_AGENT} ]]; then
    printf '%s' "${INSTALLED_AGENT}"
  else
    command -v agent 2>/dev/null || true
  fi
}

# Subagents get their own SessionStart. Collecting once per session is
# housekeeping; once per subagent is a race between them over the same worktrees.
gc::is_subagent() {
  local payload="$1" kind
  kind="$(jq -r '.agent_type // empty' <<<"${payload}" 2>/dev/null || true)"
  [[ -n ${kind} ]]
}

gc::message() {
  local cli output removed count
  cli="$(gc::agent)"
  [[ -n ${cli} ]] || return 0

  # A non-zero exit means nothing collectable, not an error worth reporting.
  output="$(timeout "${GC_TIMEOUT}" "${cli}" workspace gc 2>/dev/null || true)"
  removed="$(grep -E '^gc: removed ' <<<"${output}" || true)"
  [[ -n ${removed} ]] || return 0

  count="$(grep -c '' <<<"${removed}")"
  printf 'Collected %s spent session worktree(s):\n%s' "${count}" "${removed}"
}

main() {
  local print_only="no"
  [[ ${1:-} == "--print" ]] && print_only="yes"

  local payload message
  payload="$(cat)"

  if [[ ${print_only} == "no" ]]; then
    [[ -n ${GITHUB_ACTIONS:-} || -n ${CI:-} ]] && exit 0
    [[ ${AGENT_WORKTREE_GC:-1} == 0 ]] && exit 0
    gc::is_subagent "${payload}" && exit 0
  fi

  message="$(gc::message)"

  if [[ ${print_only} == "yes" ]]; then
    printf '%s\n' "${message}"
    exit 0
  fi

  [[ -n ${message} ]] || exit 0
  jq -n --arg msg "${message}" '{systemMessage: $msg}'
}

main "$@"
