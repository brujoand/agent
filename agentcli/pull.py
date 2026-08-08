from __future__ import annotations

from pathlib import Path

from agentcli import git, repos
from agentcli.config import repo_path, src_root
from agentcli.errors import AgentError, AgentGitError


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
    root = src_root()
    urls = repos.clone_urls()
    if not urls:
        raise AgentError("no reachable repositories")

    failed: list[str] = []
    for url in urls:
        # Pass the full `owner/name` slug and let the layout decide: flat drops
        # the owner, owner-scoped keeps it. `git clone` creates the leading
        # directory, so a new owner needs no setup.
        dest = repo_path(repos.slug(url))
        label = str(dest.relative_to(root))
        # The agent repo is an ordinary sibling now -- cloned, fast-forwarded and
        # helper-asserted like the rest. Safe because the installed CLI is an
        # isolated copy, so updating this checkout cannot pull the CLI out from
        # under the running process.
        try:
            action = sync_one(url, dest)
            print(f"==> {action} {label}")
        except AgentGitError as exc:
            print(f"==> skipping {label}: {exc}")
            failed.append(label)

    if failed:
        print(f"\npull: {len(failed)} repo(s) need attention: {' '.join(failed)}")
        return 1

    print("\npull: all repos up to date")
    return 0
