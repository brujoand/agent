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

# Interactive Claude sessions record their active worktree here, keyed by session
# id, so the shell statusline can surface it: the Bash tool resets cwd to the
# primary checkout on every call, so the session cannot infer it from cwd.
SESSION_POINTER_DIR = Path.home() / ".claude" / "session-worktrees"

# The repo whose worktrees are the common case.
DEFAULT_REPO = "gitops-homelab"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "agent"


# Where the agent root lives. Not derived from __file__: the installed CLI is an
# isolated copy under ~/.local/share/uv/tools/..., so `__file__` would resolve to
# its site-packages rather than the checkout, and `agent pull` would try to clone
# repos into a python package directory. Not derived from cwd either -- git spawns
# the credential helper from inside whatever repo it is authenticating.
#
# AGENT_ROOT overrides it; the checkout's ./agent launcher sets it to itself, so
# running from source always acts on that tree.


def default_agent_root() -> Path:
    """Resolved at call time, not import time: HOME differs between the box and the image."""
    return Path.home() / "src" / "agent"


def agent_root() -> Path:
    """The directory holding the agent checkout -- and every repo `agent pull` clones."""
    override = os.environ.get("AGENT_ROOT")
    return Path(override).expanduser().resolve() if override else default_agent_root()


def repo_path(repo: str) -> Path:
    return agent_root() / repo


def worktree_base(repo: str) -> Path:
    return Path.home() / "worktrees" / repo
