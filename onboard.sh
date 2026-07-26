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
# Onboarding does five things: install the App, apply the branch-protection
# ruleset, (public repos only) require manual approval for external fork PRs so a
# fork cannot reach CI -- and any agent secret wired into it -- unattended, set
# the agent's CLAUDE_CODE_OAUTH_TOKEN Actions secret from your environment, and
# register a `registry_package` webhook so a published container image notifies
# the in-cluster receiver, which opens the deploy PR immediately (see below).
#
# The webhook goes on EVERY onboarded repo, not just deployed ones: it is
# harmless where the repo is not deployed to gitops (the receiver's `lab bump`
# finds no matching app and reports it ignored), and it means a repo that later
# becomes a deployed artifact needs no second onboarding pass. Like the Actions
# secret, it is skipped when its env vars are unset.
#
# It refuses to run against the control repo: that repo manages its own App
# access and protections from inside the cluster, so it is never a valid target.
# (It used to also be unsafe -- the shared ruleset omitted Renovate's bypass and
# applying it there stripped Renovate's ability to merge. The definition now
# declares that actor, so the shared document is correct everywhere; only the
# self-management argument remains.)
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
# identical on every repo, so it ports cleanly.
#
# It also exempts the Renovate App (Integration, pull_request mode) so Renovate's
# automerge keeps working. An Integration actor_id is the APP id, not the
# installation id -- global to the App and therefore identical on every repo, so
# it ports exactly as cleanly as the admin role. An earlier version of this file
# claimed the opposite and kept Renovate out of the shared definition; that was
# wrong, and it made every fleet-wide apply a silent revocation.
#
# Naming that actor has a PRECONDITION: GitHub rejects a bypass actor for an App
# that is not installed on the repo, with a bare 422. So onboarding installs
# Renovate before it writes the ruleset -- the install is not an extra feature,
# it is what makes the ruleset valid. RENOVATE_INSTALLATION_ID and
# RENOVATE_BYPASS_APP_ID are therefore a pair: supply both or neither.

set -euo pipefail

# The brujoand-agent App's installation id on the brujoand account. This is a
# stable, non-secret address: it is fixed for the life of the installation
# (adding/removing repos does not change it), and the id alone grants nothing --
# minting a token requires a JWT signed by the App's private key, which is not
# here. So it is safe and correct to hardcode. It only changes if the App is
# fully uninstalled from the account and reinstalled.
readonly INSTALLATION_ID="144736354"

# The default ruleset, shared with `agent setup rulesets` -- it lives under
# agentcli/ as that command's package data (agentcli/rulesets.py reads the same
# directory), so onboarding points there rather than keeping a second copy.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
readonly SCRIPT_DIR
readonly RULESET_FILE="${SCRIPT_DIR}/agentcli/ruleset_defs/protect-main-pr-only.json"
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

# The release->bump webhook. Subscribes to `registry_package`: when the repo
# publishes a container image to GHCR, the payload (tag + digest) POSTs to the
# in-cluster receiver, which HMAC-verifies it and runs `lab bump <app> <tag>
# --digest <digest>` to open the deploy PR immediately -- rather than waiting for
# Renovate's hourly poll (Renovate stays the safety net behind it). registry_package
# is used, not `release`, because its payload carries the image digest, so the
# receiver never has to read GHCR (which is blind to PRIVATE packages). Registered
# on every onboarded repo; harmless where the repo is not deployed (the receiver
# reports an unknown app as no_app) or the tag isn't semver (ignored).
#
# Both the endpoint and the HMAC secret come from the ENVIRONMENT, never
# hardcoded. This script is public-bound, so the cluster's URL and shared secret
# must not live in it -- and keeping them parametric also means a fork of this
# tool points at its own receiver. The receiver rejects any delivery it cannot
# verify, so a webhook without a secret is pure noise: if either var is unset the
# step is skipped, exactly like the Actions secret above.
#   RELEASE_BUMP_WEBHOOK_URL     e.g. https://<receiver-host>/hook/release-bump
#   RELEASE_BUMP_WEBHOOK_SECRET  the HMAC secret the receiver verifies against
#
# Renovate, likewise from the environment and likewise a pair:
#   RENOVATE_BYPASS_APP_ID       Renovate's APP id, named as a ruleset bypass
#                                actor so its automerge survives the write
#   RENOVATE_INSTALLATION_ID     its INSTALLATION id, used to add this repo to
#                                the installation -- which is what makes that
#                                bypass actor legal, and what lets Renovate read
#                                the repo's private GHCR packages

function usage {
  cat >&2 <<EOF
usage: onboard.sh [--remove] <owner/repo>

  (default)   install the brujoand-agent App on the repo, apply the
              ${RULESET_NAME} branch-protection ruleset, (public
              repos) require approval for external fork PRs, set the
              ${AGENT_SECRET_NAME} secret from your environment, and
              register the release->bump webhook (see below).
  --remove    remove the ruleset, the release webhook, and detach the repo from
              the App. The fork-PR approval policy and the secret are left in
              place (not undone).

Refuses to run against the control repo (gitops-homelab).

Requires a human admin PAT (gh auth). Export ${AGENT_SECRET_NAME}
(from \`claude setup-token\`) to have onboarding set it; unset, that step is
skipped. Export RELEASE_BUMP_WEBHOOK_URL and RELEASE_BUMP_WEBHOOK_SECRET to
register the release webhook; unset, that step is skipped too. Idempotent: safe
to re-run -- re-running rotates both secrets.
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

# renovate_install adds the repo to the Renovate App's installation. Two things
# depend on it, and both fail obscurely without it:
#
#   1. The ruleset below names Renovate as a bypass actor, and GitHub rejects a
#      bypass actor for an App that is not installed on the repo -- a bare 422
#      with no hint as to which field is at fault. So this must run FIRST.
#   2. Renovate can only read a private GHCR package whose owning repo is inside
#      its installation. Without this, `packages: read` on the App is not enough
#      and the package silently resolves to "no-result".
#
# Same shape as app_install: the id addresses an installation, and the PAT is
# what authorises the write.
function renovate_install {
  local rid="$1"
  if [[ -z ${RENOVATE_INSTALLATION_ID:-} ]]; then
    report "renovate" "skipped (RENOVATE_INSTALLATION_ID not in env)"
    return 0
  fi
  gh api -X PUT "user/installations/${RENOVATE_INSTALLATION_ID}/repositories/${rid}" >/dev/null
  report "renovate" "installed"
}

function renovate_remove {
  local rid="$1"
  if [[ -z ${RENOVATE_INSTALLATION_ID:-} ]]; then
    report "renovate" "skipped (RENOVATE_INSTALLATION_ID not in env)"
    return 0
  fi
  gh api -X DELETE "user/installations/${RENOVATE_INSTALLATION_ID}/repositories/${rid}" >/dev/null
  report "renovate" "removed"
}

# ruleset_id echoes the id of the repo's ruleset named RULESET_NAME, or empty.
function ruleset_id {
  local slug="$1"
  gh api "repos/${slug}/rulesets" \
    --jq ".[] | select(.name == \"${RULESET_NAME}\") | .id" 2>/dev/null | head -1
}

# ruleset_document prints the ruleset with every `${VAR}` placeholder replaced by
# that environment variable, mirroring agentcli/rulesets.py `_resolve` -- the two
# consumers read the same file, so they must read it the same way.
#
# This repo is public, so an account-specific id (the Renovate App's) may not be
# committed; the definition carries the policy and the operator supplies the
# number. Digit strings become JSON numbers: GitHub rejects a string actor_id.
#
# An unset variable is fatal, never a silently omitted actor. bypass_actors is
# replaced wholesale by the PUT below, so dropping one does not leave it alone --
# it REVOKES it.
function ruleset_document {
  jq '
    walk(
      if type == "string" and test("^\\$\\{[A-Z][A-Z0-9_]*\\}$")
      then
        (ltrimstr("${") | rtrimstr("}")) as $name
        | ((env[$name] // "") | gsub("^\\s+|\\s+$"; "")) as $value
        | if $value == "" then
            error("ruleset needs $\($name), which is unset or empty. Export it, e.g. \($name)=<id> ./onboard.sh ...")
          elif ($value | test("^[0-9]+$")) then ($value | tonumber)
          else $value
          end
      else .
      end
    )
  ' "${RULESET_FILE}"
}

# ruleset_apply creates the ruleset if absent, else replaces it. GitHub 422s on a
# duplicate name, so a blind POST would fail on re-run -- match by name first.
function ruleset_apply {
  local slug="$1" id doc
  doc="$(ruleset_document)" || return 1
  id="$(ruleset_id "$slug")"
  if [[ -z $id ]]; then
    ruleset_write POST "repos/${slug}/rulesets" "$doc" || return 1
    report "ruleset" "created  ${RULESET_NAME}"
  else
    ruleset_write PUT "repos/${slug}/rulesets/${id}" "$doc" || return 1
    report "ruleset" "updated  ${RULESET_NAME}"
  fi
}

# ruleset_write is ruleset_apply's one API call, wrapped so a 422 explains
# itself. GitHub returns a bare "Validation Failed" for a bypass actor naming an
# App that is not installed on the repo -- no field, no actor, nothing to search
# for. That is the likeliest way this call fails, so say so rather than let the
# operator rediscover it.
function ruleset_write {
  local method="$1" path="$2" doc="$3" out
  if out="$(gh api -X "$method" "$path" --input - <<<"$doc" 2>&1)"; then
    return 0
  fi
  echo "${out}" >&2
  if [[ ${out} == *"Validation Failed"* || ${out} == *"422"* ]]; then
    echo "onboard.sh: the ruleset was rejected. The usual cause is a bypass actor" >&2
    echo "  naming a GitHub App that is not installed on this repo -- GitHub reports" >&2
    echo "  that as a bare 422. Check that every Integration actor in" >&2
    echo "  ${RULESET_FILE##*/} is installed here (RENOVATE_INSTALLATION_ID covers" >&2
    echo "  Renovate), and that each actor_id is an App id, not an installation id." >&2
  fi
  return 1
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

# webhook_id echoes the id of the repo's release->bump webhook, matched by its
# config.url (a repo may carry several `web` hooks, so the URL is the identity we
# own), or empty. Needs RELEASE_BUMP_WEBHOOK_URL set; callers guard that.
function webhook_id {
  local slug="$1"
  gh api "repos/${slug}/hooks" \
    --jq ".[] | select(.config.url == \"${RELEASE_BUMP_WEBHOOK_URL}\") | .id" 2>/dev/null | head -1
}

# webhook_apply registers (or, matched by URL, updates in place) the `release`
# webhook that drives release->bump. SKIPPED unless both the endpoint and the
# secret are in the environment -- the receiver rejects deliveries it cannot
# verify, so a secretless hook would only generate noise. The secret goes through
# a jq-built body on stdin, never argv. Idempotent: re-running rotates the secret.
function webhook_apply {
  local slug="$1" id
  if [[ -z ${RELEASE_BUMP_WEBHOOK_URL:-} || -z ${RELEASE_BUMP_WEBHOOK_SECRET:-} ]]; then
    report "release-hook" "skipped (RELEASE_BUMP_WEBHOOK_URL / _SECRET not in env)"
    return 0
  fi
  id="$(webhook_id "$slug")"
  if [[ -z $id ]]; then
    jq -n --arg url "$RELEASE_BUMP_WEBHOOK_URL" --arg secret "$RELEASE_BUMP_WEBHOOK_SECRET" \
      '{name: "web", active: true, events: ["registry_package"],
        config: {url: $url, content_type: "json", insecure_ssl: "0", secret: $secret}}' |
      gh api -X POST "repos/${slug}/hooks" --input - >/dev/null
    report "release-hook" "created"
  else
    jq -n --arg url "$RELEASE_BUMP_WEBHOOK_URL" --arg secret "$RELEASE_BUMP_WEBHOOK_SECRET" \
      '{active: true, events: ["registry_package"],
        config: {url: $url, content_type: "json", insecure_ssl: "0", secret: $secret}}' |
      gh api -X PATCH "repos/${slug}/hooks/${id}" --input - >/dev/null
    report "release-hook" "updated"
  fi
}

# webhook_remove deletes the release->bump webhook if present. Only the URL is
# needed to find it, so this cleans up even when the secret is not in the
# environment. Called on every --remove so detaching a repo never leaves a
# dangling hook; if the URL is not in env it cannot identify the hook and says so.
function webhook_remove {
  local slug="$1" id
  if [[ -z ${RELEASE_BUMP_WEBHOOK_URL:-} ]]; then
    report "release-hook" "skipped (RELEASE_BUMP_WEBHOOK_URL not in env)"
    return 0
  fi
  id="$(webhook_id "$slug")"
  if [[ -z $id ]]; then
    report "release-hook" "absent"
  else
    gh api -X DELETE "repos/${slug}/hooks/${id}" >/dev/null
    report "release-hook" "removed"
  fi
}

# refuse_control_repo blocks onboarding gitops-homelab, whatever the owner. That
# repo applies its OWN protections (with a Renovate bypass actor the shared
# ruleset here lacks) and manages its App access from inside the cluster; running
# this human-PAT script against it would strip that bypass and fight its
# in-cluster reconciliation. Never a valid target.
function refuse_control_repo {
  local slug="$1"
  if [[ ${slug##*/} == "gitops-homelab" ]]; then
    echo "onboard.sh: refusing to onboard ${slug} -- the control repo manages its own" >&2
    echo "  App access and protections in-cluster, and the shared ruleset here would" >&2
    echo "  strip its Renovate bypass. Never a valid target for this script." >&2
    exit 2
  fi
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
  refuse_control_repo "$slug"
  [[ -f $RULESET_FILE ]] || {
    echo "onboard.sh: missing ruleset file ${RULESET_FILE}" >&2
    exit 1
  }

  local rid
  rid="$(repo_id "$slug")"

  echo "==> ${slug}"
  if [[ $remove -eq 1 ]]; then
    # Remove the ruleset before detaching, so the write still goes through the
    # human PAT while the repo is still resolvable. The webhook is removed on
    # every --remove so a detach never orphans a hook.
    ruleset_remove "$slug"
    webhook_remove "$slug"
    renovate_remove "$rid"
    app_remove "$rid"
  else
    app_install "$rid"
    # Before ruleset_apply, not after: the ruleset names Renovate as a bypass
    # actor, which GitHub only accepts once Renovate is installed here.
    renovate_install "$rid"
    ruleset_apply "$slug"
    fork_policy_harden "$slug"
    secret_set "$slug"
    webhook_apply "$slug"
  fi
}

main "$@"
