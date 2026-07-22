#!/usr/bin/env bash
#
# Fan a single GitHub Actions secret out to every repository the App is
# installed on. Generic: give it any secret NAME and it distributes the value
# you provide. (To rotate the issue agent's Anthropic token specifically, use
# the sync-agent-secret.sh wrapper, which fixes the name.)
#
# WHY a push, not a copy: GitHub never returns an Actions secret's value -- not
# to a repo admin, not to the App. The REST API exposes only metadata (name,
# updated_at). So there is no "read it from repo A and copy to B" primitive.
# You supply the value once; this script distributes it. It is never taken as an
# argument (that would leak into shell history and the process list) -- on a
# terminal the script prompts for a hidden paste, otherwise it reads stdin.
#
# The value's SOURCE is yours to choose:
#   scripts/sync-repo-secret.sh MY_SECRET               # prompts, paste hidden
#   pass show anthropic/oauth | scripts/sync-repo-secret.sh MY_SECRET
#   scripts/sync-repo-secret.sh MY_SECRET < value.txt
#
# Target repos come from `agent repos` (the installation's own repo list), so
# the set is whatever the App can currently reach -- nothing is hardcoded.
#
# Writing secrets is a MAINTAINER action -- the App has NO secrets access at all
# (the REST secrets API 403s for its token), so the write cannot use App creds.
# `gh` picks the write credential in order:
#   * $GH_TOKEN if exported -- YOUR token that can write repo secrets (a classic
#     PAT with `repo`, or a fine-grained PAT with Secrets: read/write);
#   * otherwise your `gh auth login` session (needs admin on each target repo).
# Note the two credentials are different: even with $GH_TOKEN set to your PAT,
# `agent repos` still enumerates targets via the App key. So you can run this on
# an agent host -- App lists the repos, your PAT does the writing.
#
# Usage:
#   scripts/sync-repo-secret.sh <SECRET_NAME> [--dry-run] [--exclude owner/repo]...
set -euo pipefail

usage() {
  grep '^#' "$0" | sed '1d;s/^# \{0,1\}//'
}

name=""
dry_run=false
excludes=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      dry_run=true
      shift
      ;;
    --exclude)
      [[ $# -ge 2 ]] || {
        echo "error: --exclude needs an owner/repo argument" >&2
        exit 2
      }
      excludes+=("$2")
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      echo "error: unknown option: $1" >&2
      exit 2
      ;;
    *)
      if [[ -z $name ]]; then
        name="$1"
        shift
      else
        echo "error: unexpected argument: $1" >&2
        exit 2
      fi
      ;;
  esac
done

if [[ -z $name ]]; then
  echo "error: missing <SECRET_NAME>" >&2
  echo "run with --help for usage" >&2
  exit 2
fi

is_excluded() {
  local repo="$1" ex
  for ex in ${excludes[@]+"${excludes[@]}"}; do
    [[ $repo == "$ex" ]] && return 0
  done
  return 1
}

# Collect the target repos first so a dry run can print the plan without ever
# touching stdin (no point demanding a secret you are not going to write).
mapfile -t repos < <(agent repos | sed -E 's#^https://github.com/##; s#\.git$##')

targets=()
for repo in ${repos[@]+"${repos[@]}"}; do
  [[ -z $repo ]] && continue
  if is_excluded "$repo"; then
    echo "skip (excluded): $repo"
    continue
  fi
  targets+=("$repo")
done

if [[ ${#targets[@]} -eq 0 ]]; then
  echo "error: no target repos (is the App installed anywhere?)" >&2
  exit 1
fi

echo "secret:  $name"
echo "targets: ${#targets[@]} repo(s)"
for repo in "${targets[@]}"; do
  echo "  - $repo"
done

if [[ $dry_run == true ]]; then
  echo
  echo "dry run: no secret read, nothing written. Drop --dry-run to apply."
  exit 0
fi

# Read the value now. On a terminal, prompt for a hidden paste; do NOT ask the
# maintainer to pipe an interactive minting command (e.g. `claude setup-token`)
# in -- such a command prints its own prompts to stdout, which the pipe would
# swallow, hanging forever. When stdin is a pipe/file (automation, `< value.txt`)
# read it straight.
if [[ -t 0 ]]; then
  printf 'Paste value for %s (input hidden, then Enter): ' "$name" >&2
  read -rs value
  printf '\n' >&2
else
  value="$(cat)"
fi
if [[ -z $value ]]; then
  echo "error: empty value" >&2
  exit 2
fi

failed=()
for repo in "${targets[@]}"; do
  # Feed the value via stdin, never as --body, so it stays out of the argv of
  # the spawned gh process.
  if printf '%s' "$value" | gh secret set "$name" --repo "$repo" >/dev/null 2>&1; then
    echo "ok:   $repo"
  else
    echo "FAIL: $repo" >&2
    failed+=("$repo")
  fi
done

echo
if [[ ${#failed[@]} -gt 0 ]]; then
  echo "done with ${#failed[@]} failure(s): ${failed[*]}" >&2
  echo "(a failure usually means missing admin/secrets:write on that repo)" >&2
  exit 1
fi
echo "done: $name set on ${#targets[@]} repo(s)"
