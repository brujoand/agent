from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from agentcli import git
from agentcli.config import (
    BOT_EMAIL,
    BOT_NAME,
    SESSION_POINTER_DIR,
    agent_root,
    repo_path,
    worktree_base,
)
from agentcli.errors import AgentConfigError, AgentGitError, AgentInputError

# A session worktree is only auto-purged once it has been idle this long AND no
# live claude is anchored to it.
GC_AGE_SECONDS = int(os.environ.get("AGENT_WORKTREE_GC_AGE_SECONDS", 24 * 3600))


def _checkout(repo: str) -> Path:
    path = repo_path(repo)
    if not git.is_checkout(path):
        raise AgentConfigError(f"{path} is not a git checkout -- run `agent pull` first")
    return path


def parse_branch(branch: str) -> str:
    """`feat/alertmanager-gh-issues` -> slug `alertmanager-gh-issues`."""
    if "/" not in branch:
        raise AgentInputError(f"branch must be <type>/<slug> (e.g. feat/my-change), got {branch!r}")
    slug = branch.split("/", 1)[1]
    if not slug:
        raise AgentInputError(f"empty slug in {branch!r}")
    return slug


def worktree_for(repo: str, slug: str) -> Path:
    return worktree_base(repo) / f"session-{slug}"


def in_use(worktree: Path, proc_root: Path = Path("/proc")) -> bool:
    """True if any live process is anchored to this worktree.

    Checks each process's cwd and its cmdline: a claude session may have been
    started from elsewhere but still be operating on the worktree by path.

    Every comparison is on a directory boundary, never a bare prefix -- otherwise
    a process inside `session-xy` would mark `session-x` as in use and block its
    collection forever.
    """
    target = str(worktree)
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cwd = os.readlink(proc / "cwd")
            if cwd == target or cwd.startswith(target + "/"):
                return True
        except OSError:
            pass
        try:
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        # Pad so a path at the very end of the cmdline still terminates on a
        # boundary rather than end-of-string.
        padded = cmdline + " "
        if f"{target}/" in padded or f"{target} " in padded:
            return True
    return False


def age_seconds(worktree: Path) -> int | None:
    """Seconds since git last wrote the worktree's admin dir.

    git touches .git/worktrees/<id> on add/commit/checkout/index changes, so it
    is a good proxy for "last git activity".
    """
    result = git.run(["-C", str(worktree), "rev-parse", "--absolute-git-dir"], check=False)
    admin = result.stdout.strip()
    if not admin:
        return None
    try:
        return int(time.time() - Path(admin).stat().st_mtime)
    except OSError:
        return None


def _git_app_auth(worktree: Path) -> None:
    """Point this worktree's github.com traffic at the brujoand-agent App.

    Scoped to THIS worktree via git's worktree config: linked worktrees share the
    common .git/config, so a plain --local would leak these settings -- bot
    authorship especially -- into the primary checkout and every sibling.
    """
    git.config_set(worktree, "extensions.worktreeConfig", "true")
    git.config_set(worktree, "url.https://github.com/.insteadOf", "git@github.com:", worktree=True)
    git.set_github_helper(worktree, worktree=True)
    git.config_set(worktree, "user.name", BOT_NAME, worktree=True)
    git.config_set(worktree, "user.email", BOT_EMAIL, worktree=True)
    git.config_set(worktree, "core.hooksPath", ".githooks", worktree=True)


def _sync_primary_main(checkout: Path) -> None:
    """Best-effort fast-forward of the checkout's main. Never blocks worktree creation."""
    try:
        if git.current_branch(checkout) != "main" or git.is_dirty(checkout):
            return
        git.fast_forward(checkout)
    except AgentGitError:
        pass


def _write_session_pointer(worktree: Path) -> None:
    """Record the active worktree for the shell statusline.

    The Bash tool resets cwd to the primary checkout on every call, so a session
    can never infer its worktree from cwd -- it has to point at it. Only for real
    sessions; a human running `agent` by hand has no session id.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        return
    SESSION_POINTER_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_POINTER_DIR / session_id).write_text(f"{worktree}\n")


def _forget_session_pointers(worktree: Path) -> None:
    for pointer in _session_pointers():
        if pointer.read_text().strip() == str(worktree):
            pointer.unlink(missing_ok=True)


def _session_pointers() -> list[Path]:
    if not SESSION_POINTER_DIR.is_dir():
        return []
    return [p for p in SESSION_POINTER_DIR.iterdir() if p.is_file()]


def prune_session_pointers() -> int:
    """Drop pointers to worktrees that no longer exist.

    The bash implementation never did this, so pointers accumulated whenever a
    worktree was removed with plain `git worktree remove`.
    """
    removed = 0
    for pointer in _session_pointers():
        try:
            target = pointer.read_text().strip()
        except OSError:
            continue
        if target and not Path(target).is_dir():
            pointer.unlink(missing_ok=True)
            removed += 1
    return removed


def managed_repos() -> list[str]:
    """Every repo checkout under the agent root."""
    return sorted(p.name for p in agent_root().iterdir() if p.is_dir() and git.is_checkout(p))


def session_worktrees(repo: str) -> list[Path]:
    base = worktree_base(repo)
    if not base.is_dir():
        return []
    result = git.run(["-C", str(repo_path(repo)), "worktree", "list", "--porcelain"], check=False)
    live = {
        line.split(" ", 1)[1] for line in result.stdout.splitlines() if line.startswith("worktree ")
    }
    return sorted(p for p in base.iterdir() if p.is_dir() and str(p) in live)


def create(branch: str, repo: str) -> Path:
    checkout = _checkout(repo)
    slug = parse_branch(branch)
    worktree = worktree_for(repo, slug)
    if worktree.exists():
        raise AgentInputError(f"{worktree} already exists")

    gc(repo=repo, quiet=True)

    git.run(["-C", str(checkout), "fetch", "--quiet", "origin"])
    _sync_primary_main(checkout)

    worktree.parent.mkdir(parents=True, exist_ok=True)
    git.run(
        ["-C", str(checkout), "worktree", "add", "-b", branch, str(worktree), "origin/main"],
    )

    # New worktrees are untrusted, and the tool shims read their env from mise
    # config -- they fail until it is trusted. Best-effort: a box without mise
    # (or a repo without a mise.toml) still gets a usable worktree.
    mise = Path.home() / ".local" / "bin" / "mise"
    if mise.is_file():
        subprocess.run([str(mise), "trust", str(worktree)], capture_output=True, check=False)

    _git_app_auth(worktree)
    _write_session_pointer(worktree)
    return worktree


def delete(slug_or_branch: str, repo: str) -> Path:
    slug = slug_or_branch.split("/", 1)[1] if "/" in slug_or_branch else slug_or_branch
    worktree = worktree_for(repo, slug)
    if not worktree.exists():
        raise AgentInputError(f"no such worktree: {worktree}")

    # Never --force: git refuses to remove a worktree with uncommitted changes,
    # which is exactly the protection we want for unpushed work.
    git.run(["-C", str(repo_path(repo)), "worktree", "remove", str(worktree)])
    git.run(["-C", str(repo_path(repo)), "worktree", "prune"], check=False)
    _forget_session_pointers(worktree)
    return worktree


def gc(repo: str | None = None, quiet: bool = False) -> int:
    """Remove idle session worktrees untouched for longer than the grace period.

    Never forces, so a worktree with uncommitted or untracked changes is kept.
    Branches are left intact, so committed-but-unpushed work stays reachable.
    """
    removed = 0
    targets = [repo] if repo else managed_repos()
    for name in targets:
        if not git.is_checkout(repo_path(name)):
            continue
        for worktree in session_worktrees(name):
            if in_use(worktree):
                continue
            age = age_seconds(worktree)
            if age is None or age < GC_AGE_SECONDS:
                continue
            result = git.run(
                ["-C", str(repo_path(name)), "worktree", "remove", str(worktree)], check=False
            )
            if result.returncode != 0:
                if not quiet:
                    print(f"gc: keeping {worktree.name} (dirty)")
                continue
            _forget_session_pointers(worktree)
            removed += 1
            if not quiet:
                print(f"gc: removed {worktree.name}")
        if removed:
            git.run(["-C", str(repo_path(name)), "worktree", "prune"], check=False)

    pruned = prune_session_pointers()
    if pruned and not quiet:
        print(f"gc: pruned {pruned} orphaned session pointer(s)")
    return removed
