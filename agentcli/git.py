from __future__ import annotations

import subprocess
from pathlib import Path

from agentcli.errors import AgentGitError

# The credential-helper string persisted into every clone's .git/config. git runs
# a `!`-prefixed value as a shell command, so this is an absolute path to the
# launcher -- never a bare `agent`, which would depend on PATH at the moment git
# spawns the helper (a minimal environment, from inside an arbitrary repo).
GITHUB_HELPER_KEY = "credential.https://github.com.helper"

# It points at the INSTALLED agent (an isolated copy under ~/.local/share/uv/tools),
# never at the launcher inside the checkout. The agent CLI is its own credential
# helper for the very repo that contains it, so a helper living in tracked files
# is a bootstrap trap: `git checkout` to any commit predating the CLI deletes both
# the launcher and agentcli/, git then has no way to authenticate (there is no
# ambient credential store), and `git pull` cannot fetch the commits that would
# restore it. The escape is `git merge --ff-only origin/main`, which needs no auth.
#
# `bootstrap.sh` installs that copy with `uv tool install`. The checkout keeps its
# editable .venv, so ./agent still runs live source for development.
INSTALLED_AGENT = Path.home() / ".local" / "bin" / "agent"


def helper_spec() -> str:
    return f"!{INSTALLED_AGENT} git-credential"


def run(
    args: list[str], cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AgentGitError(
            f"git {' '.join(args)} failed (exit {result.returncode})\n{result.stderr.strip()}"
        )
    return result


def config_get(repo: Path, key: str, worktree: bool = False) -> str | None:
    scope = ["--worktree"] if worktree else []
    result = run(["-C", str(repo), "config", *scope, "--get", key], check=False)
    return result.stdout.strip() or None


def config_set(repo: Path, key: str, value: str, worktree: bool = False) -> None:
    scope = ["--worktree"] if worktree else []
    run(["-C", str(repo), "config", *scope, key, value])


def is_checkout(path: Path) -> bool:
    return (path / ".git").exists()


def set_github_helper(repo: Path, worktree: bool = False) -> None:
    config_set(repo, GITHUB_HELPER_KEY, helper_spec(), worktree=worktree)


def current_branch(repo: Path) -> str:
    return run(["-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()


def default_branch(repo: Path) -> str:
    """The repo's default branch name, from `origin/HEAD`.

    Not every repo uses `main`: of the eleven the App can reach, four (dotfiles,
    home-assistant, sbp, ...) still use `master`. Hardcoding `main` made
    `agent workspace create --repo dotfiles` fail with `invalid reference:
    origin/main`. Falls back to `main` when origin/HEAD is unset.
    """
    result = run(["-C", str(repo), "symbolic-ref", "-q", "refs/remotes/origin/HEAD"], check=False)
    ref = result.stdout.strip()
    return ref.removeprefix("refs/remotes/origin/") if ref else "main"


def is_dirty(repo: Path) -> bool:
    return bool(run(["-C", str(repo), "status", "--porcelain"]).stdout.strip())


def fast_forward(repo: Path) -> None:
    """Fetch and fast-forward the current branch. Never merges, never rebases.

    A diverged, dirty, or upstream-less branch makes `merge --ff-only` refuse --
    and that refusal is the point. Local work is never silently mangled.
    """
    run(["-C", str(repo), "fetch", "--quiet", "origin"])
    result = run(["-C", str(repo), "merge", "--ff-only", "--quiet", "@{u}"], check=False)
    if result.returncode != 0:
        raise AgentGitError("not fast-forwardable (diverged, dirty, or no upstream)")


def clone(url: str, dest: Path) -> None:
    """Clone with the helper injected for this invocation, then persist it.

    `-c` on `git clone` writes the value into the new repo's config too, but we
    set it explicitly afterwards so the intent survives any future change to
    that behaviour.
    """
    run(["-c", f"{GITHUB_HELPER_KEY}={helper_spec()}", "clone", "--quiet", url, str(dest)])
    set_github_helper(dest)
