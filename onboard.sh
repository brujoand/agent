#!/usr/bin/env bash

# onboard.sh -- add a repo to the brujoand-agent App and protect its default
# branch, or (with --remove) undo both. Run by a human with an admin PAT; needs
# only `gh` and `jq`, no agent CLI.
#
# Why this lives outside the agent CLI: onboarding establishes the App's access
# and the ruleset that constrains the App. The tool that sets the boundary must
# not sit inside the boundary -- it runs before the agent exists on a repo, and
# it must keep working even if the agent install is broken.
#
# Onboarding does four things: install the App, apply the branch-protection
# ruleset, (public repos only) require manual approval for external fork PRs so a
# fork cannot reach CI -- and any agent secret wired into it -- unattended, and
# set the agent's CLAUDE_CODE_OAUTH_TOKEN Actions secret from your environment.
#
# What the human's PAT can and cannot do (all verified against the live API):
#   * install the App on a repo:  PUT  /user/installations/{id}/repositories/{repo_id}
#   * remove it:                  DELETE  (same path)
#   * write the ruleset:          POST/PUT /repos/{slug}/rulesets
#   * set fork-PR approval:       PUT  /repos/{slug}/actions/permissions/fork-pr-contributor-approval
#   * set an Actions secret:      gh secret set (repo public key + libsodium seal)
# The PAT CANNOT list installations or read /repos/{}/installation (those need a
# token authorized to the App itself), so the installation id cannot be
# discovered at runtime -- hence the constant below. This is also why the secret
# is set per-repo at onboard time, not fanned out: nothing can enumerate "where
# the App is installed" from a human token, but onboarding already names the repo.
#
# The ruleset exempts RepositoryRole 5 (admin) via bypass_actors, so the human
# admin can still push directly. This does NOT weaken the constraint on the
# agent: brujoand-agent[bot] is not an admin, so the exemption never applies to
# it -- it still cannot merge its own PRs. Admin (id 5) is a GitHub built-in and
# identical on every repo, so it ports cleanly. gitops-homelab additionally
# exempts an Integration (Renovate) actor, but that id is per-installation and
# does not port, so it is deliberately a gitops-homelab-only exception, not part
# of the shared definition here.

set -euo pipefail

# The brujoand-agent App's installation id on the brujoand account. This is a
# stable, non-secret address: it is fixed for the life of the installation
# (adding/removing repos does not change it), and the id alone grants nothing --
# minting a token requires a JWT signed by the App's private key, which is not
# here. So it is safe and correct to hardcode. It only changes if the App is
# fully uninstalled from the account and reinstalled.
readonly INSTALLATION_ID="144736354"

# The default ruleset. Kept next to this script so onboarding needs no checkout
# layout beyond the script + its data file.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
readonly SCRIPT_DIR
readonly RULESET_FILE="${SCRIPT_DIR}/ruleset_defs/protect-main-pr-only.json"
readonly RULESET_NAME="protect-main-pr-only"

# Fork-PR approval policy. `all_external_contributors` = every fork PR from an
# outside contributor needs manual approval before any workflow runs (GitHub's
# default, first_time_contributors, auto-runs returning contributors). This is
# what keeps a fork PR from reaching CI -- and any agent secret wired into it --
# without a human clicking approve. It applies only to PUBLIC repos; the API 422s
# on private ones (which cannot be forked externally anyway), so onboarding sets
# it conditionally on visibility.
readonly FORK_APPROVAL_POLICY="all_external_contributors"

# The Actions secret the agent's workflows read to authenticate to Anthropic.
# Mint its value with `claude setup-token` and export it before onboarding.
readonly AGENT_SECRET_NAME="CLAUDE_CODE_OAUTH_TOKEN"

function usage {
  cat >&2 <<EOF
usage: onboard.sh [--remove] <owner/repo>

  (default)   install the brujoand-agent App on the repo, apply the
              ${RULESET_NAME} branch-protection ruleset, (public
              repos) require approval for external fork PRs, and set the
              ${AGENT_SECRET_NAME} secret from your environment.
  --remove    remove the ruleset and detach the repo from the App. The fork-PR
              approval policy and the secret are left in place (not undone).

Requires a human admin PAT (gh auth). Export ${AGENT_SECRET_NAME}
(from \`claude setup-token\`) to have onboarding set it; unset, that step is
skipped. Idempotent: safe to re-run -- re-running rotates the secret.
EOF
  exit 2
}

# report prints an aligned "  <field>  <status>" line.
function report {
  printf '    %-10s %s\n' "$1" "$2"
}

# repo_id resolves owner/repo to its numeric id, which the installation API needs
# instead of the slug.
function repo_id {
  local slug="$1"
  gh api "repos/${slug}" --jq '.id'
}

# app_install PUTs the repo into the installation. The PAT cannot read
# /repos/{}/installation to tell "newly added" from "already there" (that needs a
# JWT), and the PUT returns 204 either way, so the report is deliberately
# generic. Idempotent regardless.
function app_install {
  local rid="$1"
  gh api -X PUT "user/installations/${INSTALLATION_ID}/repositories/${rid}" >/dev/null
  report "app" "installed"
}

function app_remove {
  local rid="$1"
  gh api -X DELETE "user/installations/${INSTALLATION_ID}/repositories/${rid}" >/dev/null
  report "app" "removed"
}

# ruleset_id echoes the id of the repo's ruleset named RULESET_NAME, or empty.
function ruleset_id {
  local slug="$1"
  gh api "repos/${slug}/rulesets" \
    --jq ".[] | select(.name == \"${RULESET_NAME}\") | .id" 2>/dev/null | head -1
}

# ruleset_apply creates the ruleset if absent, else replaces it. GitHub 422s on a
# duplicate name, so a blind POST would fail on re-run -- match by name first.
function ruleset_apply {
  local slug="$1" id
  id="$(ruleset_id "$slug")"
  if [[ -z $id ]]; then
    gh api -X POST "repos/${slug}/rulesets" --input "${RULESET_FILE}" >/dev/null
    report "ruleset" "created  ${RULESET_NAME}"
  else
    gh api -X PUT "repos/${slug}/rulesets/${id}" --input "${RULESET_FILE}" >/dev/null
    report "ruleset" "updated  ${RULESET_NAME}"
  fi
}

function ruleset_remove {
  local slug="$1" id
  id="$(ruleset_id "$slug")"
  if [[ -z $id ]]; then
    report "ruleset" "absent"
  else
    gh api -X DELETE "repos/${slug}/rulesets/${id}" >/dev/null
    report "ruleset" "removed  ${RULESET_NAME}"
  fi
}

# fork_policy_harden requires manual approval for external fork PRs, but only on
# public repos -- the endpoint 422s on private ones. Deliberately NOT undone by
# --remove: detaching a repo from the agent is no reason to loosen a security
# control, and reverting to the permissive default would be strictly worse.
function fork_policy_harden {
  local slug="$1" visibility
  visibility="$(gh api "repos/${slug}" --jq '.visibility')"
  if [[ $visibility != "public" ]]; then
    report "fork-pr" "n/a (${visibility}; forks need no approval gate)"
    return 0
  fi
  gh api -X PUT "repos/${slug}/actions/permissions/fork-pr-contributor-approval" \
    -f approval_policy="${FORK_APPROVAL_POLICY}" >/dev/null
  report "fork-pr" "approval: ${FORK_APPROVAL_POLICY}"
}

# secret_set upserts the AGENT_SECRET_NAME Actions secret from the environment.
# `gh secret set` adds it if absent and overwrites it if present, so this is
# idempotent -- and re-running onboard is how you rotate the token. The value is
# fed on stdin, never as --body, so it stays out of the gh process's argv. If the
# env var is unset/empty it is SKIPPED, not cleared: re-onboarding for the ruleset
# alone must never clobber a good secret with a blank.
function secret_set {
  local slug="$1"
  if [[ -z ${CLAUDE_CODE_OAUTH_TOKEN:-} ]]; then
    report "secret" "skipped (\$${AGENT_SECRET_NAME} not in env)"
    return 0
  fi
  printf '%s' "$CLAUDE_CODE_OAUTH_TOKEN" |
    gh secret set "$AGENT_SECRET_NAME" --repo "$slug" >/dev/null
  report "secret" "set  ${AGENT_SECRET_NAME}"
}

function main {
  local remove=0 slug=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --remove) remove=1 ;;
      -h | --help) usage ;;
      -*)
        echo "onboard.sh: unknown option: $1" >&2
        usage
        ;;
      *)
        [[ -n $slug ]] && usage
        slug="$1"
        ;;
    esac
    shift
  done
  [[ -n $slug ]] || usage
  [[ $slug == */* ]] || {
    echo "onboard.sh: expected owner/repo, got '${slug}'" >&2
    exit 2
  }
  [[ -f $RULESET_FILE ]] || {
    echo "onboard.sh: missing ruleset file ${RULESET_FILE}" >&2
    exit 1
  }

  local rid
  rid="$(repo_id "$slug")"

  echo "==> ${slug}"
  if [[ $remove -eq 1 ]]; then
    # Remove the ruleset before detaching, so the write still goes through the
    # human PAT while the repo is still resolvable.
    ruleset_remove "$slug"
    app_remove "$rid"
  else
    app_install "$rid"
    ruleset_apply "$slug"
    fork_policy_harden "$slug"
    secret_set "$slug"
  fi
}

main "$@"
