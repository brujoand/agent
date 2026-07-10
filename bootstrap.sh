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
# PATH has neither (e.g. a fresh ansible shell). This venv is editable, so
# ./agent in the checkout always runs live source -- the development path.
(cd "$script_dir" && "$_MISE" exec -- uv sync)

# The INSTALLED agent: an isolated copy, immune to what is checked out here.
#
# This CLI is its own git credential helper for the very repo that contains it.
# If ~/.local/bin/agent pointed into the checkout, `git checkout` to any commit
# predating the CLI would delete both the launcher and agentcli/ -- and git would
# then have no way to authenticate (there is no ambient credential store), so
# `git pull` could not fetch the commits that would restore it. A self-referential
# bootstrap trap. `uv tool install` copies the package into its own environment,
# so the helper keeps working no matter what this working tree looks like.
#
# Pinned to .python-version, the same interpreter mise.toml and the CI runner
# image use, so the installed copy cannot drift onto a newer Python whose syntax
# the image would reject.
echo "==> installing the agent CLI into ${_BIN_DIR} (isolated copy)"
mkdir -p "$_BIN_DIR"
_PYTHON_VERSION="$(<"${script_dir}/.python-version")"
(cd "$script_dir" && "$_MISE" exec -- uv tool install --force --python "$_PYTHON_VERSION" .)

echo ""
echo "bootstrap.sh: agent ready at ${_BIN_DIR}/agent"
echo "  Next: agent pull && agent lab install"
