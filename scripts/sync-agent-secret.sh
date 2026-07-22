#!/usr/bin/env bash
#
# Fan a single GitHub Actions secret out to every repository the App is
# installed on. Run this as a maintainer when a shared secret rotates -- most
# commonly CLAUDE_CODE_OAUTH_TOKEN, which the central issue-agent hub and the
# per-repo pr-review workflows all read.
#
# WHY a push, not a copy: GitHub never returns an Actions secret's value -- not
# to a repo admin, not to the App. The REST API exposes only metadata (name,
# updated_at). So there is no "read it from repo A and copy to B" primitive.
# You supply the value once (on stdin); this script distributes it. Keeping the
# value on stdin also keeps it out of this tracked file and out of the process
# argument list.
#
# The value's SOURCE is yours to choose -- pipe it in from wherever it lives:
#   claude setup-token | scripts/sync-agent-secret.sh CLAUDE_CODE_OAUTH_TOKEN
#   pass show anthropic/oauth | scripts/sync-agent-secret.sh CLAUDE_CODE_OAUTH_TOKEN
#   scripts/sync-agent-secret.sh CLAUDE_CODE_OAUTH_TOKEN < token.txt
#
# Target repos come from `agent repos` (the installation's own repo list), so
# the set is whatever the App can currently reach -- nothing is hardcoded.
#
# Writing needs credentials that can set repo secrets. `gh` uses, in order:
#   * $GH_TOKEN if exported -- e.g. an App token with `secrets: write`:
#       GH_TOKEN="$(agent github token)" scripts/sync-agent-secret.sh NAME
#   * otherwise your own `gh auth login` session (needs admin on each repo).
#
# Usage:
#   scripts/sync-agent-secret.sh <SECRET_NAME> [--dry-run] [--exclude owner/repo]...
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

# Read the value now, from stdin only. Refuse an interactive terminal: without a
# pipe there is nothing to read and we would hang waiting on the maintainer.
if [[ -t 0 ]]; then
  echo "error: no value on stdin. Pipe the secret in, e.g." >&2
  echo "  claude setup-token | $0 $name" >&2
  exit 2
fi

value="$(cat)"
if [[ -z $value ]]; then
  echo "error: empty value on stdin" >&2
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
