"""The primary-checkout guard, exercised as the harness runs it: JSON on stdin, exit code out.

Testing the shell rather than a Python port is the point. The hook is the thing
that actually fires, and its failure mode is silent -- a regex that stops
matching blocks nothing and says nothing, which is indistinguishable from a
quiet week.

Exit 2 blocks the tool call; anything else lets it through.

These tests document the guard AS IT IS, including where it does not reach: a
plain shell redirection into a primary checkout is allowed today (see
`test_documents_the_redirection_gap`). That is recorded rather than asserted
away so a later widening of the regex has a test to flip.
"""

import json
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "require-worktree.sh"

# The hook keys on $HOME/src/<repo>/.git, so the fixture builds that shape under
# a tmp HOME rather than touching the real workspace.
ENV_BASE = {"PATH": "/usr/bin:/bin:/usr/local/bin"}


@pytest.fixture
def home(tmp_path):
    """A tmp HOME containing ~/src/demo (a checkout) and ~/worktrees/demo/session-x."""
    repo = tmp_path / "src" / "demo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    worktree = tmp_path / "worktrees" / "demo" / "session-x"
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    # A loose file directly under ~/src, which is NOT inside any checkout.
    (tmp_path / "src" / "CLAUDE.md").write_text("workspace notes\n")
    return tmp_path


def _run(home, tool, tool_input, cwd, env=None):
    payload = json.dumps({"tool_name": tool, "cwd": str(cwd), "tool_input": tool_input})
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env={**ENV_BASE, "HOME": str(home), **(env or {})},
    )


def _edit(home, path, cwd=None):
    return _run(home, "Edit", {"file_path": str(path)}, cwd or home)


def _bash(home, command, cwd, env=None):
    return _run(home, "Bash", {"command": command}, cwd, env)


# --- the tool-call path: Edit/Write/NotebookEdit ------------------------------


@pytest.mark.parametrize("tool", ["Edit", "Write", "NotebookEdit"])
def test_blocks_every_write_tool_into_a_primary_checkout(home, tool):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    result = _run(home, tool, {key: str(home / "src" / "demo" / "x.py")}, home)
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr


def test_allows_a_write_into_a_worktree(home):
    result = _edit(home, home / "worktrees" / "demo" / "session-x" / "x.py")
    assert result.returncode == 0


def test_allows_a_loose_file_under_src_that_is_not_a_checkout(home):
    # ~/src/CLAUDE.md belongs to no repo; blocking it would be a false positive.
    assert _edit(home, home / "src" / "CLAUDE.md").returncode == 0


def test_block_message_names_the_repo_and_the_way_out(home):
    stderr = _edit(home, home / "src" / "demo" / "x.py").stderr
    assert "~/src/demo" in stderr
    assert "agent workspace create" in stderr
    assert "--repo demo" in stderr


def test_relative_path_is_resolved_against_cwd(home):
    # A bare filename with cwd inside the checkout still lands in the checkout.
    result = _run(home, "Edit", {"file_path": "x.py"}, home / "src" / "demo")
    assert result.returncode == 2


# --- the Bash path -----------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m wip",
        "git push",
        "git add .",
        "git rebase origin/main",
        "git reset --hard",
        "git checkout -- .",
    ],
)
def test_blocks_mutating_git_in_a_primary_checkout(home, command):
    assert _bash(home, command, home / "src" / "demo").returncode == 2


@pytest.mark.parametrize(
    "command",
    ["git status", "git log --oneline", "git diff", "git merge-base HEAD main", "git fetch"],
)
def test_allows_read_only_git_in_a_primary_checkout(home, command):
    # Read-only work in ~/src is explicitly fine; `merge-base` is the one the
    # trailing word boundary in the regex exists to protect.
    assert _bash(home, command, home / "src" / "demo").returncode == 0


def test_blocks_git_c_retargeting_a_primary_checkout_from_elsewhere(home):
    result = _bash(home, f"git -C {home}/src/demo commit -m wip", home / "worktrees")
    assert result.returncode == 2


def test_blocks_cd_into_a_primary_checkout(home):
    result = _bash(home, f"cd {home}/src/demo && git commit -m wip", home / "worktrees")
    assert result.returncode == 2


def test_allows_cd_into_a_worktree_from_a_primary_cwd(home):
    # The dominant shape on this host: Bash resets cwd to the primary checkout,
    # so the command cd's out to the worktree. Must not be a false positive.
    wt = home / "worktrees" / "demo" / "session-x"
    assert _bash(home, f"cd {wt} && git commit -m wip", home / "src" / "demo").returncode == 0


def test_tilde_paths_are_expanded(home):
    assert _bash(home, "git -C ~/src/demo commit -m wip", home / "worktrees").returncode == 2


# --- escape hatches and environments -----------------------------------------


def test_documented_override_lets_the_human_through(home):
    result = _bash(
        home, "git commit -m wip", home / "src" / "demo", {"AGENT_ALLOW_PRIMARY_WRITE": "1"}
    )
    assert result.returncode == 0


@pytest.mark.parametrize("var", ["GITHUB_ACTIONS", "CI"])
def test_never_fires_in_ci(home, var):
    # GitHub-hosted runs already work in a fresh isolated checkout and must not
    # be pushed toward creating worktrees.
    result = _run(
        home, "Edit", {"file_path": str(home / "src" / "demo" / "x.py")}, home, {var: "true"}
    )
    assert result.returncode == 0


def test_unknown_tool_is_ignored(home):
    assert _run(home, "Grep", {"pattern": "x"}, home / "src" / "demo").returncode == 0


# --- known gaps, recorded --------------------------------------------------


def test_documents_the_prose_false_positive(home):
    """Text that merely LOOKS like a retarget is treated as one.

    The `cd <path>` grep runs over the whole command string, including quoted
    arguments and heredoc bodies. A commit message mentioning `cd somewhere`
    yields "somewhere" as a target, which — being relative — resolves against
    the session cwd. Since the Bash tool resets cwd to the primary checkout on
    every call, that lands in ~/src/<repo> and blocks.

    This is not hypothetical: it blocked the commit that added this file, whose
    message quoted a `cd` example. The block is safe (fails toward refusing)
    but confusing, since the command's real target was a worktree.

    A fix would resolve targets only from positions where a shell would treat
    them as one. Recorded so that change flips a test.
    """
    wt = home / "worktrees" / "demo" / "session-x"
    primary = home / "src" / "demo"

    # Baseline: cd out to the worktree from a primary cwd is allowed.
    assert _bash(home, f"cd {wt} && git commit -m x", primary).returncode == 0

    # Same command, message mentioning a cd. The prose token is collected as a
    # second target, resolves relative to the primary cwd, and blocks.
    with_prose = f"cd {wt} && git commit -m 'note: cd elsewhere retargets it'"
    result = _bash(home, with_prose, primary)
    assert result.returncode == 2, "false positive fixed -- invert this test"
    assert "~/src/demo" in result.stderr


def test_documents_the_redirection_gap(home):
    """A shell redirection into a primary checkout is NOT blocked today.

    The mutating-command regex is a verb allowlist (git verbs, `sed -i`,
    `perl -i`, `tee`); `>` and heredocs are not in it. This is the guard's
    widest accidental path -- `cat > file <<EOF` is an ordinary agent idiom.
    Asserted as-is so widening the regex flips a test instead of silently
    changing behavior nobody had pinned.
    """
    result = _bash(home, "echo hacked > x.py", home / "src" / "demo")
    assert result.returncode == 0, "gap closed -- invert this test and update the docstring"
