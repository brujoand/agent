#!/usr/bin/env bash
# PreToolUse hook: never build on a branch whose PR is already merged (or closed).
#
# A session that resumes an old worktree lands on a branch whose PR was merged
# days ago. Commits made there go nowhere: pushing does not reopen a merged PR,
# so the work sits on a dead branch and looks done when it is not.
#
# Merges here are SQUASH merges, so the branch tip is NOT an ancestor of the
# default branch and plain git ancestry misses it (13 of 16 live worktrees).
# The reliable signal is the PR state, so this hook asks GitHub -- and caches the
# answer, because MERGED is terminal.
#
# Exit 2 = block the tool call and feed stderr back to the model.
# Infra failures (no token, offline) fail OPEN: a flaky network must not wedge
# the session. The complementary always-local ancestry check still applies.
set -euo pipefail

state_dir="${HOME}/.claude/state/branch-status"
clean_ttl=900 # re-ask GitHub about a not-yet-merged branch after 15 min

[[ -n ${GITHUB_ACTIONS:-} || -n ${CI:-} ]] && exit 0
[[ ${AGENT_ALLOW_MERGED_BRANCH:-0} == 1 ]] && exit 0

payload=$(cat)
tool=$(jq -r '.tool_name // empty' <<<"${payload}" 2>/dev/null) || exit 0
cwd=$(jq -r '.cwd // empty' <<<"${payload}" 2>/dev/null) || exit 0

# Which directory does this tool call act on?
case "${tool}" in
  Edit | Write | NotebookEdit)
    file=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' <<<"${payload}")
    target=$(dirname "${file:-${cwd}}")
    ;;
  Bash)
    command=$(jq -r '.tool_input.command // empty' <<<"${payload}")
    # Only the commands that would actually add to the branch.
    writes='(^|[[:space:];&|(])(git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+(commit|push|cherry-pick|revert|am|apply)([[:space:]]|$)|gh[[:space:]]+pr[[:space:]]+create)'
    [[ ${command} =~ ${writes} ]] || exit 0
    target=$(grep -oE 'git[[:space:]]+-C[[:space:]]+[^[:space:];&|]+' <<<"${command}" |
      head -1 | sed -E 's/^git[[:space:]]+-C[[:space:]]+//') || true
    target="${target:-${cwd}}"
    ;;
  *) exit 0 ;;
esac

target="${target/#\~/${HOME}}"
[[ ${target} == /* ]] || target="${cwd}/${target}"
[[ -d ${target} ]] || exit 0

# Must be a git checkout. The primary checkouts under ~/src are require-worktree's
# job, not ours.
git -C "${target}" rev-parse --git-dir >/dev/null 2>&1 || exit 0
toplevel=$(git -C "${target}" rev-parse --show-toplevel 2>/dev/null) || exit 0
[[ ${toplevel} == "${HOME}/src/"* ]] && exit 0

branch=$(git -C "${target}" rev-parse --abbrev-ref HEAD 2>/dev/null) || exit 0
default=$(git -C "${target}" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null |
  sed 's|^origin/||') || true
default="${default:-main}"
[[ ${branch} == "HEAD" || ${branch} == "${default}" ]] && exit 0

# Repo name from the origin remote, not the directory, so it survives renames.
origin=$(git -C "${target}" remote get-url origin 2>/dev/null) || exit 0
repo=$(basename "${origin}" .git)
owner=$(basename "$(dirname "${origin}")")
owner="${owner##*:}"

hook::block() {
  local why="$1" slug="${branch##*/}"
  cat >&2 <<EOF
BLOCKED: branch '${branch}' in ${target} ${why}

Commits added here go nowhere -- pushing does NOT reopen a merged PR, so the work
would sit on a dead branch while looking done. This worktree is spent.

Start a fresh one off the current default branch:

  agent workspace delete ${slug} --repo ${repo}
  cd "\$(agent workspace create <type>/<new-slug> --repo ${repo})"

Carry uncommitted work across first, if there is any:

  git -C ${target} diff > /tmp/carry.patch   # then: git apply /tmp/carry.patch

Override (rare, human): AGENT_ALLOW_MERGED_BRANCH=1
EOF
  exit 2
}

# 1. Cached verdict. MERGED is terminal, so a hit needs no network at all.
cache_key="${state_dir}/${repo}__${branch//\//_}"
if [[ -f ${cache_key} ]]; then
  read -r status detail <"${cache_key}" || true
  case "${status}" in
    MERGED | CLOSED)
      hook::block "was already ${status,,} (${detail})."
      ;;
    CLEAN)
      age=$(($(date +%s) - $(stat -c %Y "${cache_key}" 2>/dev/null || echo 0)))
      [[ ${age} -lt ${clean_ttl} ]] && exit 0
      ;;
  esac
fi

# 2. Free local check: catches merge-commit / rebase merges with no network.
if git -C "${target}" merge-base --is-ancestor HEAD "origin/${default}" 2>/dev/null &&
  [[ $(git -C "${target}" rev-parse HEAD) != $(git -C "${target}" rev-parse "origin/${default}" 2>/dev/null) ]]; then
  mkdir -p "${state_dir}"
  printf 'MERGED already contained in origin/%s\n' "${default}" >"${cache_key}"
  hook::block "is already contained in origin/${default}."
fi

# 3. Ask GitHub. The stored gh token is stale; the App token is the live one.
token="${GH_TOKEN:-}"
[[ -n ${token} ]] || token=$(timeout 10 agent github token 2>/dev/null) || exit 0
[[ -n ${token} ]] || exit 0

pr=$(GH_TOKEN="${token}" timeout 10 gh pr list -R "${owner}/${repo}" \
  --head "${branch}" --state all --limit 1 \
  --json number,state --jq '.[0] | "\(.state) #\(.number)"' 2>/dev/null) || exit 0

mkdir -p "${state_dir}"
case "${pr}" in
  MERGED*) printf 'MERGED PR %s\n' "${pr#MERGED }" >"${cache_key}" && hook::block "was already merged as PR ${pr#MERGED }." ;;
  CLOSED*) printf 'CLOSED PR %s\n' "${pr#CLOSED }" >"${cache_key}" && hook::block "belongs to closed PR ${pr#CLOSED } (closed, not merged)." ;;
  *) printf 'CLEAN %s\n' "${pr:-no-pr}" >"${cache_key}" ;;
esac

exit 0
