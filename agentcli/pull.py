from __future__ import annotations

from pathlib import Path

from agentcli import git, repos
from agentcli.config import agent_root
from agentcli.errors import AgentError, AgentGitError


def _self_slug(root: Path) -> str | None:
    """The repo this CLI lives in, if any.

    Skipped below: cloning it into its own directory would nest agent/ inside
    agent/.
    """
    result = git.run(["-C", str(root), "remote", "get-url", "origin"], check=False)
    origin = result.stdout.strip()
    return repos.slug(origin) if origin else None


def sync_one(url: str, dest: Path) -> str:
    """Clone or fast-forward one repo. Returns 'cloned' or 'updated'.

    Always (re)asserts the credential helper, so the migration off the old
    `pull.sh credential` / `lab github git_credential` strings is self-healing:
    any repo this touches ends up pointing at `agent git-credential`.
    """
    if not dest.exists():
        git.clone(url, dest)
        return "cloned"

    if not git.is_checkout(dest):
        raise AgentGitError(f"{dest} exists but is not a git checkout")

    git.set_github_helper(dest)
    git.fast_forward(dest)
    return "updated"


def run() -> int:
    root = agent_root()
    self_slug = _self_slug(root)
    urls = repos.clone_urls()
    if not urls:
        raise AgentError("no reachable repositories")

    failed: list[str] = []
    for url in urls:
        name = repos.name(url)
        if self_slug and repos.slug(url) == self_slug:
            print(f"==> skipping {name} (this repo)")
            continue

        try:
            action = sync_one(url, root / name)
            print(f"==> {action} {name}")
        except AgentGitError as exc:
            print(f"==> skipping {name}: {exc}")
            failed.append(name)

    if failed:
        print(f"\npull: {len(failed)} repo(s) need attention: {' '.join(failed)}")
        return 1

    print("\npull: all repos up to date")
    return 0
