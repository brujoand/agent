#!/usr/bin/env bash
# PreToolUse hook: the primary checkouts under ~/src are read-only for agents.
#
# Every change that will become a PR happens in a worktree created by
# `agent workspace create` (~/worktrees/<repo>/session-<slug>), branched off a
# freshly-fetched default branch. This hook makes that mandatory instead of
# advisory: it blocks file writes and mutating git commands whose target lands
# inside ~/src/<repo>, for every repo in the workspace.
#
# Exit 2 = block the tool call and feed stderr back to the model.
# Any other nonzero exit is a non-blocking warning, so a hook bug never wedges
# the session.
set -euo pipefail

src_root="${HOME}/src"

# GitHub-hosted runs (CI, the issue agent) already execute in a fresh isolated
# checkout on their own branch and must NOT create worktrees. They never load
# this file, but belt and braces.
[[ -n ${GITHUB_ACTIONS:-} || -n ${CI:-} ]] && exit 0

# Documented escape hatch for the human: AGENT_ALLOW_PRIMARY_WRITE=1
[[ ${AGENT_ALLOW_PRIMARY_WRITE:-0} == 1 ]] && exit 0

payload=$(cat)
tool=$(jq -r '.tool_name // empty' <<<"${payload}" 2>/dev/null) || exit 0
cwd=$(jq -r '.cwd // empty' <<<"${payload}" 2>/dev/null) || exit 0

# Resolve a path against cwd and report which ~/src repo it falls inside, if any.
hook::repo_of() {
  local path="$1" rel repo
  [[ -n ${path} ]] || return 1
  path="${path/#\~/${HOME}}"
  [[ ${path} == /* ]] || path="${cwd}/${path}"
  # Normalize without requiring the path to exist (realpath -m).
  path=$(realpath -m "${path}" 2>/dev/null) || return 1

  [[ ${path} == "${src_root}/"* ]] || return 1
  rel="${path#"${src_root}/"}"
  repo="${rel%%/*}"
  # A git checkout, not a loose file like ~/src/CLAUDE.md.
  [[ -e "${src_root}/${repo}/.git" ]] || return 1
  printf '%s' "${repo}"
}

hook::block() {
  local repo="$1" what="$2"
  cat >&2 <<EOF
BLOCKED: ${what} targets ~/src/${repo}, the primary checkout.

Primary checkouts are read-only for agents. Every change lands via a worktree +
feature branch + PR — never in ~/src, and never on the default branch:

  cd "\$(agent workspace create <type>/<slug> --repo ${repo})"

That prints the worktree path, branches off a freshly-fetched origin/HEAD, trusts
mise, and wires up brujoand-agent git auth. Redo this change in there, run the
repo's pre-commit gate, open the PR, report the URL, and stop — only the human
merges.

Read-only work in ~/src is fine and is not blocked.
EOF
  exit 2
}

case "${tool}" in
  Edit | Write | NotebookEdit)
    file=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' <<<"${payload}")
    if repo=$(hook::repo_of "${file}"); then
      hook::block "${repo}" "this ${tool}"
    fi
    ;;

  Bash)
    command=$(jq -r '.tool_input.command // empty' <<<"${payload}")

    # Only commands that can mutate a checkout. Read-only git (status, log, diff,
    # show, fetch, worktree, rev-parse, ls-files, branch --show-current) stays
    # free, as does everything `agent workspace create` runs on its own.
    # The trailing boundary matters: without it `merge` also matches the
    # read-only `merge-base`.
    mutating='(^|[[:space:];&|(])(git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+(commit|push|add|rm|mv|merge|rebase|cherry-pick|revert|am|apply|stash|reset|restore|checkout|switch|clean)([[:space:]]|$)|sed[[:space:]]+-i|perl[[:space:]]+-i|tee[[:space:]])'
    [[ ${command} =~ ${mutating} ]] || exit 0

    # Where would it land? An explicit `git -C <path>` or `cd <path>` retargets the
    # command away from the session cwd; a literal ~/src path names its own target.
    targets=()
    while read -r path; do
      [[ -n ${path} ]] && targets+=("${path}")
    done < <(grep -oE '(git[[:space:]]+-C|cd)[[:space:]]+[^[:space:];&|]+' <<<"${command}" |
      sed -E 's/^(git[[:space:]]+-C|cd)[[:space:]]+//')
    while read -r path; do
      [[ -n ${path} ]] && targets+=("${path}")
    done < <(grep -oE "(${HOME}|~)/src/[^[:space:];&|'\"]+" <<<"${command}")
    # No explicit target named: the command acts on the session cwd.
    [[ ${#targets[@]} -eq 0 ]] && targets=("${cwd}")

    for path in "${targets[@]}"; do
      if repo=$(hook::repo_of "${path}"); then
        hook::block "${repo}" "this Bash command"
      fi
    done
    ;;
esac

exit 0
