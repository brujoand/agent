#!/usr/bin/env bash

set -euo pipefail

# install.sh - install the `lab` CLI from the gitops-homelab checkout under the
# agent root.
#
# Runs after pull.sh, which is what puts gitops-homelab on disk. The dependency
# points one way only: the agent root knows how to install lab, lab knows
# nothing about the agent root.
#
#   1. trust + install the repo-pinned toolchain (mise)
#   2. mirror the pinned cluster CLIs into the global mise config, so kubectl /
#      talosctl / helm resolve from any cwd, not just inside the checkout
#   3. build the lab CLI venv (uv sync) -- without it, `lab`'s Python modules
#      silently fall through to the bash dispatcher and vanish
#   4. symlink `lab` into ~/.local/bin
#
# `lab/lab` resolves its own location with `readlink -f`, so the symlink target
# decides which checkout `lab` *is* -- including which repo `lab agent workspace
# create` branches from. Pointing it here is what makes the agent root
# authoritative.

script_dir="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

_REPO="${script_dir}/gitops-homelab"
_MISE="${HOME}/.local/bin/mise"
_BIN_DIR="${HOME}/.local/bin"

# Pinned in the repo's mise.toml but only directory-scoped; mirror them globally.
_GLOBAL_TOOLS=(kubectl talosctl node gh helm flux2 kustomize yq uv)

function install::_require {
  if [[ ! -d ${_REPO}/.git ]]; then
    echo "install.sh: ${_REPO} is not a git checkout." >&2
    echo "  Run ${script_dir}/pull.sh first -- it clones gitops-homelab." >&2
    return 1
  fi
  if [[ ! -x $_MISE ]]; then
    echo "install.sh: mise not found at ${_MISE}." >&2
    return 1
  fi
}

function install::_toolchain {
  echo "==> installing repo-pinned toolchain (mise)"
  "$_MISE" trust "$_REPO"
  (cd "$_REPO" && "$_MISE" install)
}

# install::_pin_global mirrors the repo-pinned versions into the global mise
# config. The repo mise.toml is directory-scoped, so without this `talosctl`,
# `kubectl` and friends only resolve inside the checkout.
function install::_pin_global {
  echo "==> pinning cluster CLIs globally"
  local tool ver
  for tool in "${_GLOBAL_TOOLS[@]}"; do
    ver="$(cd "$_REPO" && "$_MISE" current "$tool" 2>/dev/null || true)"
    [[ -n $ver ]] && "$_MISE" use -g "${tool}@${ver}"
  done
}

# install::_venv builds the labcli venv. `lab/lab` probes for lab/.venv/bin/python
# and, when it is missing, quietly falls back to the bash dispatcher -- so the
# Python-backed modules (network, hass, opnsense, ...) disappear without an error.
function install::_venv {
  echo "==> building the lab CLI venv (uv sync)"
  local uv="${HOME}/.local/share/mise/shims/uv"
  [[ -x $uv ]] || uv="$(command -v uv)"
  (cd "${_REPO}/lab" && "$uv" sync)
}

function install::_symlink {
  echo "==> symlinking lab into ${_BIN_DIR}"
  mkdir -p "$_BIN_DIR"
  ln -sfn "${_REPO}/lab/lab" "${_BIN_DIR}/lab"
}

function install::main {
  install::_require
  install::_toolchain
  install::_pin_global
  install::_venv
  install::_symlink

  echo ""
  echo "install.sh: lab installed from ${_REPO}"
  echo "  $(readlink -f "${_BIN_DIR}/lab")"
}

install::main "$@"
