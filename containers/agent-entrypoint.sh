#!/usr/bin/env bash
# Entrypoint for the interactive agent image.
#
# One job, and it exists because of a contract the CLI makes on a workstation:
# `agentcli/git.py` persists `credential.https://github.com.helper` into every
# clone as an ABSOLUTE path to `~/.local/bin/agent`, never a bare `agent` (git
# spawns the helper from inside an arbitrary repo with a minimal environment, so
# PATH cannot be trusted). That is the right call there and this does not change
# it.
#
# In here, HOME is /mnt/bagent, so that path resolves to
# /mnt/bagent/.local/bin/agent -- which does not exist, because the CLI lives in
# the image at /usr/local/bin/agent. Every clone's persisted helper would point
# at nothing and git would have no way to authenticate.
#
# So: make the conventional path real. A symlink, not a copy, so replacing the
# image replaces the CLI it resolves to. Recreated on every start rather than
# assumed, since /mnt/bagent is a host mount that may predate this image or have
# been provisioned by hand.
set -euo pipefail

readonly INSTALLED="${HOME}/.local/bin/agent"
readonly IMAGE_AGENT=/usr/local/bin/agent

if [[ -w ${HOME} || -w $(dirname "${INSTALLED}") ]]; then
  mkdir -p "$(dirname "${INSTALLED}")"
  # -f so a stale link from an older image is replaced rather than skipped.
  ln -sfn "${IMAGE_AGENT}" "${INSTALLED}"
else
  # Not fatal: read-only or wrongly-owned HOME is a provisioning problem, and
  # saying so beats failing to start with a symlink error.
  echo "agent-entrypoint: ${HOME} is not writable -- git credential helper at" \
    "${INSTALLED} cannot be linked; check the ownership of the mount" >&2
fi

exec "$@"
