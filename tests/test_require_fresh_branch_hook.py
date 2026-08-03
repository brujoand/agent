"""The spent-worktree guard, exercised as the harness runs it: JSON on stdin, exit code out.

Merges in this workspace are SQUASH merges, so a merged branch's tip is not an
ancestor of the default branch and plain git ancestry misses it. The hook's real
signal is PR state from GitHub, cached because MERGED is terminal.

These tests never reach the network. The three paths are driven directly:
the cache (seed the state file), the local ancestry check (build the history),
and the no-token path (an empty PATH, which must fail OPEN).

Exit 2 blocks the tool call; anything else lets it through.
"""

import json
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "require-fresh-branch.sh"

# No `agent` and no `gh` on PATH: step 3 cannot reach GitHub, so anything that
# blocks in these tests blocked on the cache or on local ancestry alone.
ENV_BASE = {"PATH": "/usr/bin:/bin"}


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A worktree-shaped checkout on `feat/x`, with origin/main behind it.

    Lives under ~/worktrees (NOT ~/src) -- the hook defers anything under ~/src
    to require-worktree.sh and exits early.
    """
    path = tmp_path / "worktrees" / "demo" / "session-x"
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "commit", "-q", "--allow-empty", "-m", "base")
    _git(path, "remote", "add", "origin", "https://github.com/owner/demo.git")
    # origin/main pinned at the base commit; origin/HEAD names it the default.
    _git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(path, "checkout", "-q", "-b", "feat/x")
    _git(path, "commit", "-q", "--allow-empty", "-m", "work")
    return path


def _run(repo, tool, tool_input, env=None):
    payload = json.dumps({"tool_name": tool, "cwd": str(repo), "tool_input": tool_input})
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        # HOME is the tmp root, so the cache lives under a tmp .claude/state.
        env={**ENV_BASE, "HOME": str(repo.parents[2]), **(env or {})},
    )


def _bash(repo, command, env=None):
    return _run(repo, "Bash", {"command": command}, env)


def _seed_cache(repo, branch, line):
    state = repo.parents[2] / ".claude" / "state" / "branch-status"
    state.mkdir(parents=True, exist_ok=True)
    (state / f"demo__{branch.replace('/', '_')}").write_text(line)


# --- the cached-verdict path (no network needed) -----------------------------


@pytest.mark.parametrize("status", ["MERGED", "CLOSED"])
def test_blocks_a_branch_cached_as_finished(repo, status):
    _seed_cache(repo, "feat/x", f"{status} PR #12\n")
    result = _bash(repo, "git commit -m more")
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr


def test_a_clean_cache_entry_lets_work_through(repo):
    _seed_cache(repo, "feat/x", "CLEAN no-pr\n")
    assert _bash(repo, "git commit -m more").returncode == 0


def test_block_message_carries_both_remedies(repo):
    _seed_cache(repo, "feat/x", "MERGED PR #12\n")
    stderr = _bash(repo, "git commit -m more").stderr
    assert "agent workspace delete x --repo demo" in stderr
    assert "agent workspace create" in stderr
    # Uncommitted work must not be silently stranded.
    assert "diff > /tmp/carry.patch" in stderr
    assert "AGENT_ALLOW_MERGED_BRANCH=1" in stderr


# --- which commands it guards ------------------------------------------------


@pytest.mark.parametrize(
    "command",
    ["git commit -m x", "git push", "git cherry-pick abc", "git revert abc", "gh pr create --fill"],
)
def test_guards_the_commands_that_add_to_a_branch(repo, command):
    _seed_cache(repo, "feat/x", "MERGED PR #12\n")
    assert _bash(repo, command).returncode == 2


@pytest.mark.parametrize("command", ["git status", "git log", "git diff", "ls"])
def test_ignores_commands_that_do_not_add_to_a_branch(repo, command):
    _seed_cache(repo, "feat/x", "MERGED PR #12\n")
    assert _bash(repo, command).returncode == 0


@pytest.mark.parametrize("tool", ["Edit", "Write"])
def test_guards_file_writes_too(repo, tool):
    _seed_cache(repo, "feat/x", "MERGED PR #12\n")
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    assert _run(repo, tool, {key: str(repo / "x.py")}).returncode == 2


# --- the free local ancestry check -------------------------------------------


def test_blocks_when_head_is_already_contained_in_the_default_branch(repo):
    # A non-squash merge: origin/main advances past this branch's tip.
    _git(repo, "update-ref", "refs/remotes/origin/main", "feat/x")
    _git(repo, "checkout", "-q", "feat/x")
    _git(repo, "reset", "-q", "--hard", "HEAD~1")
    result = _bash(repo, "git commit -m more")
    assert result.returncode == 2
    assert "already contained in origin/main" in result.stderr


def test_the_default_branch_itself_is_never_blocked(repo):
    _git(repo, "checkout", "-q", "main")
    assert _bash(repo, "git commit -m x").returncode == 0


# --- fail-open and scope -----------------------------------------------------


def test_fails_open_when_github_is_unreachable(repo):
    """No token, no gh, no cache: a flaky network must not wedge the session.

    The complementary local ancestry check still runs, so this is a narrowed
    guard rather than no guard.
    """
    assert _bash(repo, "git commit -m more").returncode == 0


def test_defers_primary_checkouts_to_the_worktree_guard(tmp_path):
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "t")
    _git(src, "commit", "-q", "--allow-empty", "-m", "base")
    _git(src, "remote", "add", "origin", "https://github.com/owner/demo.git")
    _git(src, "checkout", "-q", "-b", "feat/x")
    payload = json.dumps(
        {"tool_name": "Bash", "cwd": str(src), "tool_input": {"command": "git commit -m x"}}
    )
    result = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env={**ENV_BASE, "HOME": str(tmp_path)},
    )
    assert result.returncode == 0


def test_documented_override_lets_the_human_through(repo):
    _seed_cache(repo, "feat/x", "MERGED PR #12\n")
    assert _bash(repo, "git commit -m more", {"AGENT_ALLOW_MERGED_BRANCH": "1"}).returncode == 0


@pytest.mark.parametrize("var", ["GITHUB_ACTIONS", "CI"])
def test_never_fires_in_ci(repo, var):
    _seed_cache(repo, "feat/x", "MERGED PR #12\n")
    assert _bash(repo, "git commit -m more", {var: "true"}).returncode == 0


# --- known gaps, recorded --------------------------------------------------


def test_documents_the_cd_target_gap(repo):
    """`cd <worktree> && git commit` is NOT seen by this guard today.

    Bash target extraction understands `git -C` but not `cd`, so a cd-prefixed
    command falls back to the payload cwd. That is the dominant command shape on
    this host (the Bash tool resets cwd to the primary checkout every call), and
    a cwd under ~/src is then handed to require-worktree.sh at line 54 -- so the
    merged-branch check never runs. require-worktree.sh already greps cd targets;
    lifting that here is the fix. Recorded so closing it flips a test.
    """
    _seed_cache(repo, "feat/x", "MERGED PR #12\n")
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "cwd": str(repo.parents[2] / "src" / "demo"),
            "tool_input": {"command": f"cd {repo} && git commit -m more"},
        }
    )
    result = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env={**ENV_BASE, "HOME": str(repo.parents[2])},
    )
    assert result.returncode == 0, "gap closed -- invert this test and update the docstring"
