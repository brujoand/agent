from __future__ import annotations

from pathlib import Path

from agentcli import git, github, repos, workspace
from agentcli.config import DEFAULT_REPO, PRIVATE_ENV, agent_root, repo_path
from agentcli.creds import load_app_creds
from agentcli.errors import AgentError

_OK = "ok  "
_BAD = "FAIL"


def _line(status: str, label: str, detail: str) -> None:
    print(f"  {status}  {label:<8} {detail}")


def _check_creds() -> bool:
    try:
        load_app_creds()
    except AgentError as exc:
        _line(_BAD, "creds", str(exc).splitlines()[0])
        return False
    _line(_OK, "creds", str(PRIVATE_ENV))
    return True


def _check_token() -> bool:
    try:
        github.token()
    except AgentError as exc:
        _line(_BAD, "token", str(exc).splitlines()[0])
        return False
    _line(_OK, "token", f"expires in {github.token_expires_in() // 60}m")
    return True


def _check_repos() -> bool:
    try:
        urls = repos.clone_urls()
    except AgentError as exc:
        _line(_BAD, "repos", str(exc).splitlines()[0])
        return False
    _line(_OK, "repos", f"{len(urls)} reachable")
    return True


def _check_lab() -> bool:
    binary = repo_path(DEFAULT_REPO) / "lab" / "lab"
    if not binary.is_file():
        _line(_BAD, "lab", f"not found at {binary} -- run `agent pull && agent lab install`")
        return False
    venv = repo_path(DEFAULT_REPO) / "lab" / ".venv" / "bin" / "python"
    if not venv.is_file():
        _line(_BAD, "lab", "venv missing -- lab's Python modules would silently vanish")
        return False
    _line(_OK, "lab", str(binary))
    return True


def _check_helpers() -> bool:
    """Every managed checkout must authenticate through `agent git-credential`.

    Doubles as the migration linter: a checkout still naming `lab github
    git_credential` or `pull.sh credential` breaks the moment that code is
    deleted, and there is no ambient credential store to fall back on.
    """
    expected = git.helper_spec()
    stale: list[str] = []

    checkouts = [repo_path(name) for name in workspace.managed_repos()]
    legacy_primary = Path.home() / "src" / "gitops-homelab"
    if git.is_checkout(legacy_primary):
        checkouts.append(legacy_primary)

    for checkout in checkouts:
        actual = git.config_get(checkout, git.GITHUB_HELPER_KEY)
        if actual != expected:
            stale.append(f"{checkout.name}: {actual or '<unset>'}")

    for repo in workspace.managed_repos():
        for worktree in workspace.session_worktrees(repo):
            actual = git.config_get(worktree, git.GITHUB_HELPER_KEY, worktree=True)
            if actual != expected:
                stale.append(f"{worktree.name}: {actual or '<unset>'}")

    if stale:
        _line(_BAD, "helpers", f"{len(stale)} stale -- run `agent pull`")
        for entry in stale:
            print(f"           {entry}")
        return False
    _line(_OK, "helpers", f"{len(checkouts)} checkouts point at agent git-credential")
    return True


def run() -> int:
    print(f"agent root: {agent_root()}\n")
    checks = [_check_creds, _check_token, _check_repos, _check_lab, _check_helpers]
    results = [check() for check in checks]
    print()
    if all(results):
        print("doctor: all checks passed")
        return 0
    print(f"doctor: {results.count(False)} check(s) failed")
    return 1
