#!/usr/bin/env bash

set -euo pipefail

# pull.sh - clone or update every repo the brujoand-agent App can reach.
#
# This script is the bootstrap step for the agent root: it runs BEFORE any other
# repo exists on disk, including gitops-homelab (which carries the `lab` CLI).
# It therefore depends on nothing but bash, git, curl, jq and openssl -- no
# `lab`, no `gh`, no SSH key. Everything it needs, it does itself:
#
#   1. mints a short-lived brujoand-agent App installation token (RS256 JWT ->
#      /app/installations/:id/access_tokens), cached until shortly before expiry;
#   2. asks that installation which repos it can see
#      (/installation/repositories);
#   3. clones the missing ones into this script's directory, fast-forwards the
#      rest.
#
# It is also its own git credential helper: cloned repos get
# `credential.https://github.com.helper` pointed back at `pull.sh credential`,
# so later `git pull` in any of them re-mints without help from lab.
#
# App credentials come from the environment, falling back to ~/.bash_private,
# where `lab agent bootstrap` bakes them. There is no 1Password fallback: the
# agent host has no OP_SERVICE_ACCOUNT_TOKEN and never will.
#
# Usage:
#   pull.sh                 clone/update every reachable repo
#   pull.sh credential get  git credential helper (used internally)

_PRIVATE_ENV="${HOME}/.bash_private"
_CACHE_FILE="${XDG_CACHE_HOME:-${HOME}/.cache}/agent/github-app-token.json"
_API="https://api.github.com"

script_dir="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

# pull::_load_app_creds exports APP_ID / APP_INSTALLATION_ID /
# LAB_GH_APP_PRIVATE_KEY, sourcing ~/.bash_private when they are not already in
# the environment. Sourcing here (rather than relying on the login shell) is what
# lets git invoke the credential helper from a bare subprocess.
function pull::_load_app_creds {
  if [[ -z ${APP_ID:-} || -z ${APP_INSTALLATION_ID:-} || -z ${LAB_GH_APP_PRIVATE_KEY:-} ]] &&
    [[ -r ${_PRIVATE_ENV} ]]; then
    # shellcheck source=/dev/null
    source "${_PRIVATE_ENV}"
  fi

  if [[ -z ${APP_ID:-} || -z ${APP_INSTALLATION_ID:-} || -z ${LAB_GH_APP_PRIVATE_KEY:-} ]]; then
    echo "pull.sh: missing App credentials (APP_ID / APP_INSTALLATION_ID / LAB_GH_APP_PRIVATE_KEY)." >&2
    echo "  Expected in the environment or ${_PRIVATE_ENV}; a human runs 'lab agent bootstrap' to provision them." >&2
    return 1
  fi
}

function pull::_b64url { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# pull::_jwt signs the ~9min RS256 assertion GitHub trades for an installation
# token. iat is back-dated 5 min: a host clock running ahead of GitHub's gets a
# 401 "'iat' is in the future" otherwise.
function pull::_jwt {
  local pem_file="$1" now iat exp header payload signature
  now="$(date +%s)"
  iat="$((now - 300))"
  exp="$((now + 540))"
  header="$(printf '%s' '{"alg":"RS256","typ":"JWT"}' | pull::_b64url)"
  payload="$(printf '%s' "{\"iat\":${iat},\"exp\":${exp},\"iss\":\"${APP_ID}\"}" | pull::_b64url)"
  signature="$(printf '%s' "${header}.${payload}" |
    openssl dgst -sha256 -sign "$pem_file" -binary | pull::_b64url)"
  printf '%s.%s.%s' "$header" "$payload" "$signature"
}

# pull::_mint exchanges a JWT for an installation token. Retries transient
# failures (5xx, 429, transport) with exponential backoff + jitter; a 4xx means
# bad JWT / wrong installation / revoked App and will never recover, so it fails
# fast rather than burning the backoff budget.
function pull::_mint {
  pull::_load_app_creds || return 1

  local pem_file
  pem_file="$(mktemp)"
  chmod 600 "$pem_file"
  # shellcheck disable=SC2064  # expand pem_file now, not at trap time
  trap "rm -f '${pem_file}'" RETURN

  # The baked value is base64-wrapped PEM; tolerate a raw PEM too.
  if ! printf '%s' "${LAB_GH_APP_PRIVATE_KEY}" | base64 -d >"$pem_file" 2>/dev/null ||
    ! head -c 11 "$pem_file" | grep -q -- "-----BEGIN"; then
    printf '%s' "${LAB_GH_APP_PRIVATE_KEY}" >"$pem_file"
  fi
  if ! head -c 11 "$pem_file" | grep -q -- "-----BEGIN"; then
    echo "pull.sh: LAB_GH_APP_PRIVATE_KEY is neither a raw nor base64 PEM" >&2
    return 1
  fi

  local jwt url attempt max_attempts=5 resp curl_rc code body msg backoff token=""
  jwt="$(pull::_jwt "$pem_file")"
  url="${_API}/app/installations/${APP_INSTALLATION_ID}/access_tokens"

  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    curl_rc=0
    resp="$(curl -sS -w $'\n%{http_code}' --connect-timeout 5 --max-time 20 -X POST \
      -H "Authorization: Bearer ${jwt}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "$url")" || curl_rc=$?

    if [[ $curl_rc -ne 0 ]]; then
      code="000"
      body="{\"message\":\"curl transport error (exit ${curl_rc})\"}"
    else
      code="$(printf '%s' "$resp" | tail -n1)"
      body="$(printf '%s' "$resp" | sed '$d')"
    fi

    if [[ $code == "201" ]]; then
      token="$(printf '%s' "$body" | jq -r '.token')"
      break
    fi

    msg="$(printf '%s' "$body" | jq -r '.message // "?"' 2>/dev/null || echo '?')"
    echo "pull.sh: mint attempt ${attempt}/${max_attempts} HTTP ${code}: ${msg}" >&2

    if [[ $code != "000" && $code != "429" && $code =~ ^4[0-9][0-9]$ ]]; then
      echo "pull.sh: HTTP ${code} is non-retryable; giving up" >&2
      break
    fi

    if [[ $attempt -lt $max_attempts ]]; then
      backoff=$((2 ** (attempt - 1)))
      [[ $backoff -gt 30 ]] && backoff=30
      sleep "${backoff}.$(printf '%03d' "$((RANDOM % 1000))")"
    fi
  done

  if [[ -z $token || $token == "null" ]]; then
    echo "pull.sh: failed to obtain an installation token" >&2
    return 1
  fi
  printf '%s' "$token"
}

# pull::_token returns a cached token while it still has >5 min of life
# (installation tokens last ~1h), minting a fresh one otherwise. Without the
# cache, git would re-mint once per repo per clone.
function pull::_token {
  local now cached_token cached_exp
  now="$(date +%s)"

  if [[ -f $_CACHE_FILE ]]; then
    cached_token="$(jq -r '.token // empty' "$_CACHE_FILE" 2>/dev/null || true)"
    cached_exp="$(jq -r '.expires_at // 0' "$_CACHE_FILE" 2>/dev/null || true)"
    if [[ -n ${cached_token:-} && ${cached_exp:-0} -gt $((now + 300)) ]]; then
      printf '%s' "$cached_token"
      return 0
    fi
  fi

  local token cache_dir tmp_cache
  token="$(pull::_mint)" || return 1

  cache_dir="$(dirname "$_CACHE_FILE")"
  mkdir -p "$cache_dir"
  chmod 700 "$cache_dir"
  tmp_cache="$(mktemp "${cache_dir}/.tok.XXXXXX")"
  chmod 600 "$tmp_cache"
  jq -n --arg t "$token" --argjson e "$((now + 3600))" '{token: $t, expires_at: $e}' >"$tmp_cache"
  mv -f "$tmp_cache" "$_CACHE_FILE"

  printf '%s' "$token"
}

# pull::_credential is a git credential helper (gitcredentials(7)). Only `get` is
# meaningful: the token is short-lived and already cached, so store/erase no-op.
function pull::_credential {
  [[ ${1:-} == "get" ]] || return 0

  # Drain git's key=value request so it never takes SIGPIPE mid-write. The
  # attributes are ignored: the token is host-scoped and the username for an App
  # token is always the literal "x-access-token".
  local _line
  while IFS= read -r _line && [[ -n $_line ]]; do :; done

  local token
  token="$(pull::_token)" || return 1
  printf 'username=x-access-token\npassword=%s\n' "$token"
}

# pull::_repos prints one HTTPS clone URL per line for every repo the
# installation can see. Pages explicitly (per_page=100) rather than trusting the
# current install list to fit on one page.
function pull::_repos {
  local token page=1 body count
  token="$(pull::_token)" || return 1

  while :; do
    body="$(curl -sS --connect-timeout 5 --max-time 20 \
      -H "Authorization: Bearer ${token}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "${_API}/installation/repositories?per_page=100&page=${page}")" || return 1

    count="$(printf '%s' "$body" | jq -r '.repositories | length' 2>/dev/null || echo "null")"
    if [[ $count == "null" ]]; then
      echo "pull.sh: unexpected response listing installation repositories:" >&2
      printf '%s\n' "$body" | jq -r '.message // .' >&2 2>/dev/null || printf '%s\n' "$body" >&2
      return 1
    fi

    printf '%s' "$body" | jq -r '.repositories[].clone_url'
    [[ $count -lt 100 ]] && break
    ((page++))
  done
}

function pull::_slug {
  sed -E 's#^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)##; s#\.git$##' <<<"$1"
}

# pull::_self_slug is the repo this script lives in, if any. It is skipped below:
# cloning it into its own script dir would nest agent/ inside agent/.
function pull::_self_slug {
  local origin
  origin="$(git -C "$script_dir" remote get-url origin 2>/dev/null)" || return 0
  pull::_slug "$origin"
}

# pull::_helper_spec is the credential.helper value written into every clone: a
# `!`-prefixed shell command git runs, pointing back at this script.
function pull::_helper_spec {
  printf '!%s/pull.sh credential' "$script_dir"
}

# pull::_sync brings one repo up to date. Existing checkouts only fast-forward: a
# merge or rebase here could silently mangle local work, so a diverged (or dirty,
# or upstream-less) branch is reported and left alone.
function pull::_sync {
  local url="$1" dest="$2" name="$3" helper
  helper="$(pull::_helper_spec)"

  if [[ ! -e $dest ]]; then
    echo "==> cloning ${name}"
    git -c "credential.https://github.com.helper=${helper}" clone --quiet "$url" "$dest"
    git -C "$dest" config "credential.https://github.com.helper" "$helper"
    return 0
  fi

  if [[ ! -d ${dest}/.git ]]; then
    echo "==> skipping ${name}: ${dest} exists but is not a git repo" >&2
    return 1
  fi

  echo "==> updating ${name}"
  git -C "$dest" config "credential.https://github.com.helper" "$helper"
  git -C "$dest" fetch --quiet origin

  if ! git -C "$dest" merge --ff-only --quiet '@{u}' 2>/dev/null; then
    echo "    not fast-forwardable (diverged, dirty, or no upstream) -- left alone" >&2
    return 1
  fi
}

function pull::main {
  local self_slug urls=() url slug name failed=()
  self_slug="$(pull::_self_slug)"

  mapfile -t urls < <(pull::_repos)
  if [[ ${#urls[@]} -eq 0 ]]; then
    echo "pull.sh: no reachable repositories" >&2
    return 1
  fi

  for url in "${urls[@]}"; do
    slug="$(pull::_slug "$url")"
    name="${slug##*/}"

    if [[ -n $self_slug && $slug == "$self_slug" ]]; then
      echo "==> skipping ${name} (this repo)"
      continue
    fi

    pull::_sync "$url" "${script_dir}/${name}" "$name" || failed+=("$name")
  done

  if [[ ${#failed[@]} -gt 0 ]]; then
    echo "" >&2
    echo "pull.sh: ${#failed[@]} repo(s) need attention: ${failed[*]}" >&2
    return 1
  fi

  echo ""
  echo "pull.sh: all repos up to date"
}

case "${1:-}" in
  credential)
    shift
    pull::_credential "$@"
    ;;
  "") pull::main ;;
  *)
    echo "Usage: pull.sh [credential get]" >&2
    exit 1
    ;;
esac
