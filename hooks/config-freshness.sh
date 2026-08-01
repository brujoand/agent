#!/usr/bin/env bash
# Warn at session start when this host is not loading the config that is on origin.
#
# Installed and wired by `agent hooks install` (declaration: hooks/hooks.json):
#   SessionStart -> once per session, before the first prompt
#
# Why this exists. The distribution trees are symlinked INTO ~/src/agent, which
# is what makes `agent pull` update every session on the host with no reinstall.
# The same property makes staleness silent: a checkout twelve commits behind
# resolves every link cleanly, so every probe passes while the rules actually
# loaded are the old ones. And pulling alone is not enough either -- it updates
# the contents of what is already linked, but a skill or rule added upstream
# since install last ran has no link at all. Neither failure announces itself.
#
# So the check runs at the one moment it can still change what the session does:
# before the first prompt. `agent freshness` is the whole implementation; this
# script only decides when to ask and how to say it.
#
# Output goes out as a `systemMessage`, which reaches the human and NOT the
# model's context. That is the point: SessionStart stdout would be injected into
# every session's context, and a warning about config drift is for the person who
# can run `agent pull`, not for the model that cannot.
#
# Never blocks. `agent freshness` compares against the refs already on disk and
# refreshes in the background, and the call is bounded anyway -- a hung session
# start is worse than a missed warning.
#
# Test seam: `config-freshness.sh --print < payload.json` prints the message it
# would emit (empty when current) and touches no state.
set -e

# Hard bound on the whole check. Generous for a no-network comparison, short
# enough that a wedged git or a stale NFS mount cannot hold up a session.
readonly CHECK_TIMEOUT=10

# The INSTALLED CLI, never ./agent from a checkout -- same rule as the credential
# helper, and for the same reason: a hook that depends on tracked files breaks
# exactly when the checkout is the thing that is broken.
readonly INSTALLED_AGENT="${HOME}/.local/bin/agent"

freshness::agent() {
  if [[ -x $INSTALLED_AGENT ]]; then
    printf '%s' "$INSTALLED_AGENT"
  else
    command -v agent 2>/dev/null || true
  fi
}

# Subagents get their own SessionStart. One warning per session is a warning; one
# per subagent is noise the reader learns to skip, and the fix is the same
# `agent pull` either way -- so only the main session asks.
freshness::is_subagent() {
  local payload="$1"
  local kind
  kind="$(jq -r '.agent_type // empty' <<<"$payload" 2>/dev/null || true)"
  [[ -n $kind ]]
}

freshness::message() {
  local cli output
  cli="$(freshness::agent)"
  if [[ -z $cli ]]; then
    # No CLI, nothing to say. An uninstalled agent is not drift, and a session
    # that cannot find it is not a session this hook can help.
    return 0
  fi
  # `--line` is already the finished sentence, so this relays it verbatim rather
  # than reformatting -- two places that word the same warning is two places for
  # it to drift. Exit 1 means drift and is the expected path, so it must not trip
  # `set -e`; an older CLI with no `freshness` command prints nothing and is
  # silently tolerated, which is the right failure mode for a startup hook.
  output="$(timeout "$CHECK_TIMEOUT" "$cli" freshness --line 2>/dev/null || true)"
  if [[ -z ${output//[[:space:]]/} ]]; then
    return 0
  fi
  printf '%s' "$output"
}

main() {
  local print_only="no"
  if [[ ${1:-} == "--print" ]]; then
    print_only="yes"
  fi

  local payload message
  payload="$(cat)"

  if [[ $print_only == "no" ]] && freshness::is_subagent "$payload"; then
    exit 0
  fi

  message="$(freshness::message)"

  if [[ $print_only == "yes" ]]; then
    printf '%s\n' "$message"
    exit 0
  fi

  if [[ -z $message ]]; then
    exit 0
  fi

  jq -n --arg msg "$message" '{systemMessage: $msg}'
}

main "$@"
