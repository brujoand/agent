from __future__ import annotations

import os
from pathlib import Path

from agentcli.errors import AgentConfigError

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


# How checkouts are arranged under `src_root()`:
#
#   flat   <root>/<repo>          the workstation layout, and the default
#   owner  <root>/<owner>/<repo>  what the container mounts at /mnt/src
#
# Owner-scoped exists because /mnt/src is shared with a human who checks out
# repos from more than one owner, and two owners may use the same repo name.
# Chosen by env rather than sniffed from disk: a layout inferred from whatever
# happens to be cloned would silently change meaning as repos are added.
SRC_LAYOUT_OWNER = "owner"


def src_layout() -> str:
    return os.environ.get("AGENT_SRC_LAYOUT", "flat").strip().lower()


def _owner_scoped(root: Path, repo: str) -> Path:
    """Find `<root>/<owner>/<repo>` for a bare repo name.

    Deliberately the same rule `bagent` applies on the host side of the container
    boundary: a bare name works while it is unique, and an ambiguous one is
    refused rather than resolved by directory order. One name, one meaning, from
    either side of the mount.

    A name matching nothing returns `<root>/<repo>/<repo>` -- a path that cannot
    exist without the scan above having found it, so it is safely absent, which
    is exactly what the flat layout yields for a repo that is not cloned yet.
    Callers already test `.is_dir()` and report "run `agent pull` first"; raising
    here would turn that into a stack trace.
    """
    try:
        found = sorted(
            owner / repo for owner in root.iterdir() if owner.is_dir() and (owner / repo).is_dir()
        )
    except OSError:
        found = []

    if len(found) == 1:
        return found[0]
    if not found:
        return root / repo / repo
    owners = ", ".join(f"{path.parent.name}/{repo}" for path in found)
    raise AgentConfigError(
        f"{repo!r} is ambiguous under {root}: {owners}. Name the owner, as `<owner>/{repo}`."
    )


def repo_path(repo: str) -> Path:
    """The checkout for a repo, named bare (`agent`) or qualified (`brujoand/agent`)."""
    root = src_root()
    if src_layout() != SRC_LAYOUT_OWNER:
        # A qualified name still means one directory in the flat layout: the
        # repo's own. This keeps `agent pull` able to pass a slug in either mode.
        return root / repo.rsplit("/", 1)[-1]
    if "/" in repo:
        return root / repo
    return _owner_scoped(root, repo)


def claude_config_root() -> Path:
    """The user's Claude config dir. Honors CLAUDE_CONFIG_DIR like Claude Code does.

    Lives here rather than in one installer because several of them now write
    under it (hooks, skills, settings), and a second copy of this rule is a second
    place for a moved CLAUDE_CONFIG_DIR to be missed.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base).expanduser() if base else Path.home() / ".claude"


def worktree_base(repo: str) -> Path:
    """Where a repo's session worktrees live.

    Mirrors the checkout's own shape under `src_root()`, so the bare and the
    qualified name of one repo always land on the same base -- otherwise
    `workspace create` and `workspace gc` could disagree about where a worktree
    is depending on how the repo was named on the command line. In the container
    HOME is the agent's mounted home, so this resolves under it and persists
    across image rebuilds like the rest of that mount.
    """
    return Path.home() / "worktrees" / repo_path(repo).relative_to(src_root())


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
