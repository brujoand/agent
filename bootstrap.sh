#!/usr/bin/env bash

set -euo pipefail

# bootstrap.sh - make the `agent` CLI runnable on a bare box.
#
# The only shell script in this repo, and the only step that cannot be an `agent`
# subcommand: it is what brings `agent` into existence. Everything after this is
# Python (`agent pull`, `agent lab install`, ...).
#
# Requires only mise, which is installed independently of any repo via
# `curl mise.run | sh`. That is what keeps the bootstrap acyclic:
#
#   mise (curl)  ->  agent (this script)  ->  agent pull  ->  agent lab install
#
# Idempotent: safe to re-run.

script_dir="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
_MISE="${HOME}/.local/bin/mise"
_BIN_DIR="${HOME}/.local/bin"

if [[ ! -x ${_MISE} ]]; then
  echo "bootstrap.sh: mise not found at ${_MISE}" >&2
  echo "  Install it first: curl -fsSL https://mise.run | sh" >&2
  exit 1
fi

echo "==> trusting and installing pinned tools (python, uv)"
"$_MISE" trust "$script_dir"
(cd "$script_dir" && "$_MISE" install)

echo "==> building the agent venv (uv sync)"
# Run uv through mise so the pinned uv/python are used even when the caller's
# PATH has neither (e.g. a fresh ansible shell).
(cd "$script_dir" && "$_MISE" exec -- uv sync)

echo "==> symlinking agent into ${_BIN_DIR}"
mkdir -p "$_BIN_DIR"
ln -sfn "${script_dir}/agent" "${_BIN_DIR}/agent"

echo ""
echo "bootstrap.sh: agent ready at ${_BIN_DIR}/agent"
echo "  Next: agent pull && agent lab install"
