"""What is already in flight on a repo, before a session starts changing it.

`freshness.py` answers "is my base current". This answers a different question
that cost real work before it existed: **is someone already doing this?**

The case that motivated it. On 2026-07-29 an agent session opened #59, raising
`autoCompactWindow` to 450k. On 2026-08-01 another session was asked for the same
change, found the workstation's settings drifted from the declaration, wrote the
same change from scratch as #63, and merged it. #59 -- three days of nobody's
work, but a reviewed and mergeable PR -- was left permanently conflicted, and the
reason it had to be abandoned rather than rebased is that every line it touched
had already been changed underneath it.

Pulling `main` would not have prevented that, which is the point worth keeping.
The base *was* current. The duplicated work was never on `main` at all -- it was
sitting in an open pull request, which is exactly where `git` cannot see it.

So this asks GitHub, not the checkout: which pull requests are open, on which
branches, touching which files. Plus the local half -- session worktrees that
exist on disk -- because an abandoned worktree from a previous session is the
same hazard one layer closer.

The same query answers a second question that also cost real work: is any open PR
**stacked** on another? Squash merges delete the parent branch and rewrite its
commits, so merging a parent before its child leaves the child with a base that no
longer exists -- conflicted or closed, and the work looks merged. Three PRs went
that way. `stack_lines` states the surviving merge order for each one it finds.

## This one is for the model, not the human

The other two SessionStart hooks emit a `systemMessage`, which reaches the human
and costs no context. This one injects into the model's context instead, and the
asymmetry is deliberate: the human is not the actor about to open a duplicate PR.
An agent that cannot see #59 will write #59 again, however clearly the terminal
said otherwise.

That is a real context cost, so it is bounded: `LIMIT` pull requests, one short
line each, and nothing at all when the repo is quiet.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agentcli import freshness, git, github, workspace
from agentcli.config import repo_path

# Enough to see a crowded repo, few enough that a session start never pays for a
# wall of text. A repo with more open PRs than this has a bigger problem than
# duplicate work, and the count is reported so the truncation is never silent.
LIMIT = 10

# Files listed per PR. The question a session needs answered is "does this
# overlap what I am about to touch", and the first few paths answer it.
FILES_PER_PR = 4

GH_TIMEOUT = 15.0


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    branch: str
    files: list[str]
    total_files: int
    base: str = ""

    def line(self) -> str:
        shown = ", ".join(self.files)
        more = (
            f" +{self.total_files - len(self.files)} more"
            if self.total_files > len(self.files)
            else ""
        )
        touching = f" — touches {shown}{more}" if shown else ""
        return f"#{self.number} [{self.branch}] {self.title}{touching}"


def _gh_json(args: list[str], repo: str) -> object | None:
    """Run `gh` with a minted App token. None on any failure -- this is advisory.

    A session must start whether or not GitHub is reachable, so every failure
    path here is "say nothing", never "raise".
    """
    try:
        token = github.token()
    except Exception:  # noqa: BLE001 -- no credentials is a fact, not an error here
        return None
    # Inherit the caller's environment and override only the token. `gh` is a
    # mise shim here, not a system package, so a hand-built PATH does not find it
    # -- and the failure is silent by design, which makes it the expensive kind
    # of bug to leave in.
    env = {**os.environ, "GH_TOKEN": token}
    try:
        result = subprocess.run(  # noqa: S603
            ["gh", *args, "--repo", repo],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=GH_TIMEOUT,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return None


def open_pull_requests(slug: str, limit: int = LIMIT) -> list[PullRequest]:
    """Open PRs on `slug` (owner/repo), newest first. Empty when nothing is open."""
    data = _gh_json(
        [
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,headRefName,baseRefName,files",
        ],
        slug,
    )
    if not isinstance(data, list):
        return []
    found: list[PullRequest] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        files = [f.get("path", "") for f in item.get("files") or [] if isinstance(f, dict)]
        files = [f for f in files if f]
        found.append(
            PullRequest(
                number=int(item.get("number", 0)),
                title=str(item.get("title", "")).strip(),
                branch=str(item.get("headRefName", "")),
                files=files[:FILES_PER_PR],
                total_files=len(files),
                base=str(item.get("baseRefName", "")),
            )
        )
    return found


def stacked(prs: list[PullRequest], default: str) -> list[tuple[PullRequest, PullRequest | None]]:
    """`(child, parent)` for every open PR based on something other than `default`.

    `parent` is the open PR whose head branch that base is, or None when the base
    is a branch with no open PR of its own -- already the dangerous state, since
    nothing is left to merge in the right order.
    """
    by_branch = {pr.branch: pr for pr in prs if pr.branch}
    return [
        (pr, by_branch.get(pr.base)) for pr in prs if pr.base and pr.base != default and pr.branch
    ]


def stack_lines(prs: list[PullRequest], default: str) -> list[str]:
    """One line per stacked PR, naming the merge order that keeps it alive.

    Stated as an instruction rather than an observation: the failure is not
    "nobody noticed the stack", it is noticing and merging the parent anyway.
    """
    lines: list[str] = []
    for child, parent in stacked(prs, default):
        parent_ref = f"#{parent.number}" if parent else f"'{child.base}'"
        lines.append(
            f"  STACKED: #{child.number} is based on {parent_ref}, not {default} — "
            f"merge #{child.number} BEFORE {parent_ref}, or switch its base to {default} "
            f"once {parent_ref} lands. Merging {parent_ref} first drops #{child.number}."
        )
    return lines


def slug_for(repo: str) -> str | None:
    """`owner/repo` for a managed repo name, or None. See `git.slug`."""
    return git.slug(repo_path(repo))


def local_worktrees(repo: str) -> list[str]:
    """Session worktrees already on disk for this repo, by branch."""
    found: list[str] = []
    for tree in workspace.session_worktrees(repo):
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(tree), "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        branch = result.stdout.strip()
        if branch:
            found.append(branch)
    return sorted(found)


def report(repo: str) -> list[str]:
    """Lines describing everything in flight on `repo`. Empty when it is quiet."""
    lines: list[str] = []
    slug = slug_for(repo)
    if slug:
        prs = open_pull_requests(slug)
        if prs:
            lines.append(f"{len(prs)} open pull request(s) on {slug}:")
            lines.extend(f"  {pr.line()}" for pr in prs)
            lines.extend(stack_lines(prs, git.default_branch(repo_path(repo))))

    branches = local_worktrees(repo)
    if branches:
        lines.append(f"{len(branches)} session worktree(s) already on disk: {', '.join(branches)}")
    return lines


def repo_for(path: Path | None = None) -> str | None:
    """The managed repo name containing `path` (default cwd), or None.

    Delegates to `freshness.checkout_for`, so "which repo am I in" has one
    answer -- including its exclusion of worktrees, which matters less here but
    should not disagree between two commands that ask the same question.
    """
    checkout = freshness.checkout_for(path or Path.cwd())
    return checkout.name if checkout else None


def context_block(repo: str) -> str:
    """The text injected into a session's context. Empty when nothing is in flight.

    Ends with the instruction rather than leaving the model to infer one: the
    failure this exists to stop is not "did not know", it is "knew and did not
    connect it to the task".
    """
    lines = report(repo)
    if not lines:
        return ""
    return (
        "Work already in flight on this repo — check for overlap BEFORE writing "
        "code, and extend or rebase the existing branch instead of opening a "
        "second PR for the same change:\n" + "\n".join(lines)
    )
