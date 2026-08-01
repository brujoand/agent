#!/usr/bin/env bash
# PreToolUse hook: never open a pull request based on another feature branch.
#
# A stacked PR -- base = some other open PR's head branch -- does not survive its
# parent being merged. Merges here are SQUASH merges with branch deletion, so the
# parent's commits never reach the default branch under their original SHAs. The
# child is left with a base that no longer exists: GitHub's auto-retarget leaves
# it conflicted or closes it outright, and the work looks merged while having
# landed nowhere. Three pull requests were lost that way before this hook existed.
#
# The rule the loss taught: a stacked PR must be rebased onto the default branch
# and merged BEFORE its parent, or have its base switched to the default branch
# after the parent lands. Merging parent-first drops the child every time.
#
# Enforcing merge order is impossible from here -- only the human merges. So this
# enforces the half that is enforceable, at the only moment an agent controls:
# the stack is not created in the first place. Deliberate stacks are still
# reachable with AGENT_ALLOW_STACKED_PR=1, and `agent inflight` reports them so
# the human sees the ordering constraint before merging either one.
#
# Exit 2 = block the tool call and feed stderr back to the model.
# Everything unrecognised fails OPEN: this must never wedge a session over a
# command it merely failed to parse.
set -euo pipefail

hook::verdict() { # <reason>  -- honours --print, otherwise blocks
  if [[ ${print_only} == 1 ]]; then
    printf 'block: %s\n' "$1"
    exit 0
  fi
  cat >&2 <<EOF
BLOCKED: this pull request would be stacked on '${base}', not '${default}'.

A squash merge of the parent deletes '${base}' and rewrites its commits, leaving
this PR with a base that no longer exists. GitHub then conflicts or closes it --
the work looks merged and has landed nowhere. That has cost three PRs here.

Base it on the default branch instead:

  git rebase --onto origin/${default} ${base}
  gh pr create --base ${default} ...

If this PR genuinely cannot stand alone, opt in deliberately and say so in the
body, so the merge order survives being forgotten:

  AGENT_ALLOW_STACKED_PR=1 gh pr create --base ${base} ...
  # body must state: "Stacked on #<parent> -- merge this BEFORE #<parent>,
  # or switch this base to ${default} once #<parent> lands."
EOF
  exit 2
}

print_only=0
[[ ${1:-} == "--print" ]] && print_only=1

[[ -n ${GITHUB_ACTIONS:-} || -n ${CI:-} ]] && exit 0
[[ ${AGENT_ALLOW_STACKED_PR:-0} == 1 ]] && exit 0

payload=$(cat)
tool=$(jq -r '.tool_name // empty' <<<"${payload}" 2>/dev/null) || exit 0
[[ ${tool} == "Bash" ]] || exit 0
cwd=$(jq -r '.cwd // empty' <<<"${payload}" 2>/dev/null) || exit 0
command=$(jq -r '.tool_input.command // empty' <<<"${payload}") || exit 0

# Only the two commands that can set a PR's base. `gh api` can too, but parsing
# a REST body out of a shell string is guesswork -- and guesswork that blocks is
# worse than a gap that does not.
# Held in a variable, not inlined: an unbalanced `(` inside a bracket expression
# breaks bash's parse of `[[ =~ ]]` itself.
opens_pr='(^|[[:space:];&|(])gh[[:space:]]+pr[[:space:]]+(create|edit)([[:space:]]|$)'
[[ ${command} =~ ${opens_pr} ]] || exit 0

# --base X | --base=X | -B X. Anything else means "repo default", which is fine.
base=$(grep -oE '(--base[=[:space:]]+|-B[[:space:]]+)[^[:space:];&|)]+' <<<"${command}" |
  head -1 | sed -E 's/^(--base[=[:space:]]+|-B[[:space:]]+)//') || true
base="${base//\"/}"
base="${base//\'/}"
[[ -n ${base} ]] || {
  [[ ${print_only} == 1 ]] && echo "allow: no explicit base"
  exit 0
}

# The default branch, read from the checkout the call runs in. Repos here differ
# (`main` and `master`), so it is never assumed.
target="${cwd:-${PWD}}"
default=$(git -C "${target}" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null |
  sed 's|^origin/||') || true
if [[ -z ${default} ]]; then
  # No checkout to ask, or no origin/HEAD. "Stacked" is then unknowable, and a
  # block on a guess is worse than the gap: fail open.
  [[ ${print_only} == 1 ]] && echo "allow: no origin/HEAD in ${target}"
  exit 0
fi

if [[ ${base} == "${default}" || ${base} == "origin/${default}" ]]; then
  [[ ${print_only} == 1 ]] && echo "allow: ${base} is the default branch"
  exit 0
fi

hook::verdict "${base} is not ${default}"
