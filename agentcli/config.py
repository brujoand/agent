from __future__ import annotations

import os
from pathlib import Path

# The brujoand-agent App creds live here, baked by `lab agent bootstrap` (run by
# a human with 1Password access). This file is the ENTIRE contract between lab
# and agent -- never a code path. The agent host has no OP_SERVICE_ACCOUNT_TOKEN
# and never will.
PRIVATE_ENV = Path.home() / ".bash_private"

GITHUB_API = "https://api.github.com"

# brujoand-agent[bot] identity for agent-worktree commits. The `<id>+<login>@`
# noreply form is what makes GitHub attribute the commits to the bot account
# (same mechanism as github-actions[bot]).
BOT_NAME = "brujoand-agent[bot]"
BOT_EMAIL = "300433439+brujoand-agent[bot]@users.noreply.github.com"

# The unprivileged OS user Claude Code runs as, and the only account holding App
# credentials. Human-only commands ask *it* which repos the App reaches rather
# than reading its secrets (see rulesets.fleet).
AGENT_USER = os.environ.get("AGENT_USER", "claude")

# Interactive Claude sessions record their active worktree here, keyed by session
# id, so the shell statusline can surface it: the Bash tool resets cwd to the
# primary checkout on every call, so the session cannot infer it from cwd.
SESSION_POINTER_DIR = Path.home() / ".claude" / "session-worktrees"

# The repo whose worktrees are the common case.
DEFAULT_REPO = "gitops-homelab"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "agent"


# Where the checkouts live. `agent pull` clones every reachable repo here, as
# SIBLINGS -- the agent repo itself is just one of them.
#
# They used to be nested inside the agent checkout, which forced an inverted
# .gitignore (a gitleaks blind spot at the repo root), an inverted .dockerignore
# (the build context was 111M of unrelated checkouts), a self-skip in `pull`, and
# a special case in `doctor`. `git clean -xffd` in the agent repo would also have
# taken every sibling with it, unpushed work included.
#
# Not derived from __file__: the installed CLI is an isolated copy under
# ~/.local/share/uv/tools/..., so `__file__` resolves to its site-packages. Not
# derived from cwd either -- git spawns the credential helper from inside whatever
# repo it is authenticating.
#
# AGENT_SRC_ROOT overrides it; the checkout's ./agent launcher sets it to its own
# parent, so running from source acts on the tree that source lives in.


def default_src_root() -> Path:
    """Resolved at call time, not import time: HOME differs between the box and the image."""
    return Path.home() / "src"


def src_root() -> Path:
    """The directory holding every managed checkout, the agent repo included."""
    override = os.environ.get("AGENT_SRC_ROOT")
    return Path(override).expanduser().resolve() if override else default_src_root()


def repo_path(repo: str) -> Path:
    return src_root() / repo


def worktree_base(repo: str) -> Path:
    return Path.home() / "worktrees" / repo


# --- agent-access: step-ca SSH certificates -------------------------------
#
# Agents mint short-lived SSH certificates and log in as an unprivileged user.
# Every deployment-specific value comes from the environment (baked into the
# agent's private env by its bootstrap) -- no CA endpoint, fingerprint, domain or
# account name is hardcoded here, so this repo stays free of any one deployment.
#
# The CA URL and root fingerprint have NO defaults on purpose: without them the
# SSH commands fail closed with a clear message rather than trusting a wrong CA.
STEP_CA_URL = os.environ.get("STEP_CA_URL")
STEP_CA_FINGERPRINT = os.environ.get("STEP_CA_FINGERPRINT")

# The JWK provisioner that signs baseline certs and the env var holding its
# password (provisioned out-of-band, same contract as the App key).
STEP_CA_PROVISIONER = os.environ.get("STEP_CA_PROVISIONER", "agent-baseline")
STEP_CA_PROVISIONER_PW_VAR = "STEP_CA_PROVISIONER_PASSWORD"

# The principal a baseline cert carries and the OS user it logs into.
SSH_BASELINE_PRINCIPAL = os.environ.get("STEP_CA_SSH_PRINCIPAL", "agent-baseline")
AGENT_SSH_USER = os.environ.get("AGENT_SSH_USER", "agent")

# Default cert lifetime. Kept at/under the provisioner's maxUserSSHCertDuration.
SSH_CERT_TTL = os.environ.get("STEP_CA_SSH_TTL", "1h")


def ssh_dir() -> Path:
    """Agent-owned dir (0700) for the baseline key/cert -- not the user's ~/.ssh."""
    return cache_dir() / "ssh"


def ssh_key_path() -> Path:
    return ssh_dir() / "agent_access_key"


def ssh_cert_path() -> Path:
    # step writes the cert alongside the key as <key>-cert.pub.
    return ssh_dir() / "agent_access_key-cert.pub"


def step_root_path() -> Path:
    return ssh_dir() / "step_root_ca.crt"
