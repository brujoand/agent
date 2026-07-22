#!/usr/bin/env bash
#
# Fan a single GitHub Actions secret out to every repository you own. Generic:
# give it any secret NAME and it distributes the value you provide. (To rotate
# the issue agent's Anthropic token specifically, use the sync-agent-secret.sh
# wrapper, which fixes the name.)
#
# This is a MAINTAINER setup script: it runs entirely on YOUR `gh` credentials
# and never uses the agent App key or the `agent` CLI. (The App cannot help
# here anyway -- it has no secrets access at all; the REST secrets API 403s for
# its token. Secret writes are inherently a human action.) So the only thing you
# need is `gh auth login` (or $GH_TOKEN set to a token that can write repo
# secrets -- a classic PAT with `repo`, or a fine-grained PAT with Secrets: r/w).
#
# WHY a push, not a copy: GitHub never returns an Actions secret's value; the
# REST API exposes only metadata (name, updated_at). So there is no "read it
# from repo A and copy to B" primitive. You supply the value once; this script
# distributes it. It is never taken as an argument (that would leak into shell
# history and the process list) -- on a terminal the script prompts for a hidden
# paste, otherwise it reads stdin.
#
# The value's SOURCE is yours to choose:
#   scripts/sync-repo-secret.sh MY_SECRET               # prompts, paste hidden
#   pass show anthropic/oauth | scripts/sync-repo-secret.sh MY_SECRET
#   scripts/sync-repo-secret.sh MY_SECRET < value.txt
#
# Targets are EXACTLY the repos where the App is installed -- not every repo you
# own. You name the App with --app <slug> (or $AGENT_APP_SLUG); GitHub resolves
# its installed repos from your gh token via the user-installations API. Review
# with --dry-run, trim with --exclude.
#
# Usage:
#   scripts/sync-repo-secret.sh <SECRET_NAME> --app <slug> [--dry-run] [--exclude owner/repo]...
set -euo pipefail

usage() {
  grep '^#' "$0" | sed '1d;s/^# \{0,1\}//'
}

name=""
dry_run=false
excludes=()
app_slug="${AGENT_APP_SLUG:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      dry_run=true
      shift
      ;;
    --app)
      [[ $# -ge 2 ]] || {
        echo "error: --app needs an app-slug argument" >&2
        exit 2
      }
      app_slug="$2"
      shift 2
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

# Enumerate the repos where the App (--app <slug> / $AGENT_APP_SLUG) is
# installed, as owner/name, one per line -- NOT every repo you own. GitHub
# answers this for your OWN gh token via the user-installations API, so no App
# key is needed: /user/installations gives the installation id for the app, and
# /user/installations/{id}/repositories gives exactly its repos.
list_repos() {
  if [[ -z $app_slug ]]; then
    echo "error: no app slug. Pass --app <slug> or set AGENT_APP_SLUG so this" >&2
    echo "  scopes to the repos where that App is installed (not all your repos)." >&2
    return 1
  fi
  local id
  id="$(gh api --paginate /user/installations \
    --jq ".installations[] | select(.app_slug==\"$app_slug\") | .id" 2>/dev/null | head -n1)"
  if [[ -z $id ]]; then
    echo "error: no installation of app '$app_slug' visible to your gh account." >&2
    echo "  is gh logged in ('gh auth login'), and is that the right --app slug?" >&2
    echo "  apps you can see:" >&2
    gh api --paginate /user/installations --jq '.installations[].app_slug' 2>/dev/null |
      sed 's/^/    /' >&2
    return 1
  fi
  gh api --paginate "/user/installations/$id/repositories" \
    --jq '.repositories[].full_name'
}

# Collect the target repos first so a dry run can print the plan without ever
# touching stdin (no point demanding a secret you are not going to write). Abort
# on a list_repos failure (bad/missing --app, gh not logged in) -- its own error
# already explains why, so don't fall through to a second "no target repos".
if ! repos_raw="$(list_repos)"; then
  exit 1
fi
mapfile -t repos <<<"$repos_raw"

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
  echo "error: no target repos -- app '$app_slug' has no installed repos (after excludes)." >&2
  exit 1
fi

echo "app:     $app_slug"
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
