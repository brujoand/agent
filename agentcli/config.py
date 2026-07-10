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
