"""The stacked-PR guard, exercised as the harness runs it: JSON on stdin, exit code out.

Testing the shell rather than a Python port is the point. The hook is the thing
that actually fires, and its failure mode is silent -- a regex that stops matching
blocks nothing and says nothing, which is indistinguishable from a quiet week.

Exit 2 blocks the tool call; anything else lets it through.
"""

import json
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "require-unstacked-pr.sh"


@pytest.fixture
def repo(tmp_path):
    """A checkout whose `origin/HEAD` says the default branch is `main`."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        ],
        check=True,
    )
    return tmp_path


def _run(command, cwd, env=None):
    payload = json.dumps({"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd), **(env or {})},
    )


def test_blocks_a_pr_based_on_another_feature_branch(repo):
    result = _run("gh pr create --base feat/parent --fill", repo)
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr


def test_the_block_names_both_remedies(repo):
    """Knowing it is wrong is not enough -- the message has to carry the way out."""
    stderr = _run("gh pr create --base feat/parent", repo).stderr
    assert "git rebase --onto origin/main feat/parent" in stderr
    assert "AGENT_ALLOW_STACKED_PR=1" in stderr


def test_allows_the_default_branch_as_a_base(repo):
    assert _run("gh pr create --base main --fill", repo).returncode == 0


def test_allows_a_pr_with_no_explicit_base(repo):
    """No `--base` means the repo default, which is the thing being asked for."""
    assert _run("gh pr create --fill", repo).returncode == 0


def test_allows_retargeting_a_stacked_pr_back_onto_the_default(repo):
    """`gh pr edit --base main` IS the fix; blocking it would trap the stack."""
    assert _run("gh pr edit 64 --base main", repo).returncode == 0


def test_blocks_the_short_flag_and_the_equals_form(repo):
    assert _run("gh pr create -B feat/parent", repo).returncode == 2
    assert _run("gh pr create --base=feat/parent", repo).returncode == 2


@pytest.mark.parametrize(
    "command",
    ["git commit -m x", "git push", "gh pr list --base feat/parent", "gh pr view 64"],
)
def test_ignores_commands_that_cannot_set_a_base(repo, command):
    assert _run(command, repo).returncode == 0


def test_the_escape_hatch_lets_a_deliberate_stack_through(repo):
    result = _run("gh pr create --base feat/parent", repo, env={"AGENT_ALLOW_STACKED_PR": "1"})
    assert result.returncode == 0


def test_ci_is_never_blocked(repo):
    """CI opens PRs from an isolated checkout and does not load these hooks anyway."""
    assert _run("gh pr create --base feat/parent", repo, env={"CI": "1"}).returncode == 0


def test_fails_open_when_the_default_branch_is_unknowable(tmp_path):
    """No origin/HEAD means "stacked" cannot be decided. A block on a guess is worse
    than the gap it closes."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert _run("gh pr create --base feat/parent", tmp_path).returncode == 0


def test_fails_open_outside_a_checkout(tmp_path):
    assert _run("gh pr create --base feat/parent", tmp_path / "nope").returncode == 0
