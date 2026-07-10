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
# What the human's PAT can and cannot do (all verified against the live API):
#   * install the App on a repo:  PUT  /user/installations/{id}/repositories/{repo_id}
#   * remove it:                  DELETE  (same path)
#   * write the ruleset:          POST/PUT /repos/{slug}/rulesets
# The PAT CANNOT list installations or read /repos/{}/installation (those need a
# token authorized to the App itself), so the installation id cannot be
# discovered at runtime -- hence the constant below.

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

function usage {
  cat >&2 <<EOF
usage: onboard.sh [--remove] <owner/repo>

  (default)   install the brujoand-agent App on the repo and apply the
              ${RULESET_NAME} branch-protection ruleset.
  --remove    remove the ruleset and detach the repo from the App.

Requires a human admin PAT (gh auth). Idempotent: safe to re-run.
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

function main {
  local remove=0 slug=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --remove) remove=1 ;;
      -h | --help) usage ;;
      -*) echo "onboard.sh: unknown option: $1" >&2; usage ;;
      *) [[ -n $slug ]] && usage; slug="$1" ;;
    esac
    shift
  done
  [[ -n $slug ]] || usage
  [[ $slug == */* ]] || { echo "onboard.sh: expected owner/repo, got '${slug}'" >&2; exit 2; }
  [[ -f $RULESET_FILE ]] || { echo "onboard.sh: missing ruleset file ${RULESET_FILE}" >&2; exit 1; }

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
  fi
}

main "$@"
