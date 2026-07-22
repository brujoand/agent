#!/usr/bin/env bash
#
# Rotate the issue agent's Anthropic credential (CLAUDE_CODE_OAUTH_TOKEN) across
# every repo the App is installed on. A thin wrapper over sync-repo-secret.sh
# with the secret name fixed -- when the token expires, every agent run fails to
# authenticate ("401 Invalid bearer token"), so it has to be refreshed in one go.
#
# Mint a fresh token FIRST (interactive -- it opens a browser), on its own:
#   claude setup-token
# then run this and paste the token at the hidden prompt:
#   scripts/sync-agent-secret.sh
#
# Flags (--dry-run, --exclude owner/repo) pass straight through. Value handling,
# target selection, and credentials are all sync-repo-secret.sh's job.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$here/sync-repo-secret.sh" CLAUDE_CODE_OAUTH_TOKEN "$@"
