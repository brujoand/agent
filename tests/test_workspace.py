from __future__ import annotations

from pathlib import Path

import pytest

from agentcli import workspace
from agentcli.errors import AgentInputError


def test_parse_branch_extracts_slug():
    assert workspace.parse_branch("feat/alertmanager-gh-issues") == "alertmanager-gh-issues"
    assert workspace.parse_branch("fix/a/b") == "a/b"


@pytest.mark.parametrize("bad", ["nosuchtype", "feat/"])
def test_parse_branch_rejects_malformed(bad):
    with pytest.raises(AgentInputError):
        workspace.parse_branch(bad)


def test_worktree_path_is_repo_namespaced(monkeypatch, tmp_path):
    monkeypatch.setattr(workspace, "worktree_base", lambda repo: tmp_path / "worktrees" / repo)
    assert (
        workspace.worktree_for("dotfiles", "x") == tmp_path / "worktrees" / "dotfiles" / "session-x"
    )


def _fake_proc(root: Path, pid: str, cwd: Path | None = None, cmdline: str = "") -> None:
    proc = root / pid
    proc.mkdir(parents=True)
    if cwd is not None:
        (proc / "cwd").symlink_to(cwd)
    (proc / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode())


def test_in_use_detects_process_with_cwd_inside(tmp_path):
    target = tmp_path / "session-x"
    (target / "sub").mkdir(parents=True)
    proc_root = tmp_path / "proc"
    _fake_proc(proc_root, "100", cwd=target / "sub")
    assert workspace.in_use(target, proc_root) is True


def test_in_use_detects_worktree_named_in_cmdline(tmp_path):
    target = tmp_path / "session-x"
    target.mkdir()
    proc_root = tmp_path / "proc"
    # Path at the very end of the cmdline must still match.
    _fake_proc(proc_root, "101", cmdline=f"claude --cwd {target}")
    assert workspace.in_use(target, proc_root) is True


def test_in_use_does_not_match_sibling_prefix(tmp_path):
    """A process in `session-xy` must not pin `session-x`, or gc never collects it."""
    target = tmp_path / "session-x"
    sibling = tmp_path / "session-xy"
    target.mkdir()
    sibling.mkdir()
    proc_root = tmp_path / "proc"
    _fake_proc(proc_root, "102", cwd=sibling, cmdline=f"claude {sibling}")
    assert workspace.in_use(target, proc_root) is False
    assert workspace.in_use(sibling, proc_root) is True


def test_in_use_false_when_nothing_anchored(tmp_path):
    target = tmp_path / "session-x"
    target.mkdir()
    proc_root = tmp_path / "proc"
    _fake_proc(proc_root, "103", cwd=tmp_path, cmdline="bash")
    (proc_root / "self").mkdir()  # non-numeric entries are skipped
    assert workspace.in_use(target, proc_root) is False


def test_prune_session_pointers_drops_dangling(monkeypatch, tmp_path):
    pointers = tmp_path / "session-worktrees"
    pointers.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    (pointers / "alive").write_text(f"{live}\n")
    (pointers / "dead").write_text(f"{tmp_path / 'gone'}\n")

    monkeypatch.setattr(workspace, "SESSION_POINTER_DIR", pointers)
    assert workspace.prune_session_pointers() == 1
    assert (pointers / "alive").exists()
    assert not (pointers / "dead").exists()


def test_prune_is_noop_without_pointer_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(workspace, "SESSION_POINTER_DIR", tmp_path / "absent")
    assert workspace.prune_session_pointers() == 0


def test_age_seconds_none_for_non_worktree(tmp_path):
    assert workspace.age_seconds(tmp_path) is None


def test_default_branch_reads_origin_head(monkeypatch, tmp_path):
    """Four of the eleven managed repos use `master`, not `main`."""
    monkeypatch.setattr(
        workspace.git,
        "run",
        lambda args, cwd=None, check=True: type(
            "R", (), {"stdout": "refs/remotes/origin/master\n", "returncode": 0}
        )(),
    )
    assert workspace.git.default_branch(tmp_path) == "master"


def test_default_branch_falls_back_to_main(monkeypatch, tmp_path):
    monkeypatch.setattr(
        workspace.git,
        "run",
        lambda args, cwd=None, check=True: type("R", (), {"stdout": "", "returncode": 1})(),
    )
    assert workspace.git.default_branch(tmp_path) == "main"


class _Response:
    """Minimal stand-in for the httpx response `github.api_get` returns."""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _pull(branch: str, merged: bool):
    return {"head": {"ref": branch}, "merged_at": "2026-08-01T00:00:00Z" if merged else None}


def test_merged_branches_lists_only_what_actually_merged(monkeypatch, tmp_path):
    """A closed-but-unmerged PR leaves real work on its branch -- never collect it."""
    monkeypatch.setattr(workspace.git, "slug", lambda path: "o/r")
    monkeypatch.setattr(workspace, "repo_path", lambda repo: tmp_path)
    monkeypatch.setattr(
        workspace.github,
        "api_get",
        lambda path, params=None: _Response([_pull("done", True), _pull("abandoned", False)]),
    )
    assert workspace.merged_branches("agent") == {"done"}


@pytest.mark.parametrize(
    "boom",
    [RuntimeError("no credentials"), OSError("offline")],
)
def test_merged_branches_is_empty_when_github_cannot_answer(monkeypatch, tmp_path, boom):
    """gc then falls back to the age rule, which is what it did before this existed."""

    def raise_it(path, params=None):
        raise boom

    monkeypatch.setattr(workspace.git, "slug", lambda path: "o/r")
    monkeypatch.setattr(workspace, "repo_path", lambda repo: tmp_path)
    monkeypatch.setattr(workspace.github, "api_get", raise_it)
    assert workspace.merged_branches("agent") == set()


def test_merged_branches_is_empty_without_a_remote(monkeypatch, tmp_path):
    monkeypatch.setattr(workspace.git, "slug", lambda path: None)
    monkeypatch.setattr(workspace, "repo_path", lambda repo: tmp_path)
    assert workspace.merged_branches("agent") == set()


def _collect_setup(monkeypatch, *, in_use=False, branch="feat/x", age=0):
    monkeypatch.setattr(workspace, "in_use", lambda worktree: in_use)
    monkeypatch.setattr(workspace.git, "current_branch", lambda path: branch)
    monkeypatch.setattr(workspace, "age_seconds", lambda worktree: age)


def test_a_merged_worktree_is_collectable_immediately(monkeypatch, tmp_path):
    """Spent the moment the PR merges: pushing to that branch reopens nothing."""
    _collect_setup(monkeypatch, age=0)
    assert workspace._collectable(tmp_path, {"feat/x"}) == "merged"


def test_an_unmerged_fresh_worktree_is_kept(monkeypatch, tmp_path):
    _collect_setup(monkeypatch, age=0)
    assert workspace._collectable(tmp_path, set()) is None


def test_an_unmerged_idle_worktree_is_still_collectable(monkeypatch, tmp_path):
    _collect_setup(monkeypatch, age=workspace.GC_AGE_SECONDS + 1)
    assert workspace._collectable(tmp_path, set()) == "idle"


def test_a_worktree_in_use_is_never_collected(monkeypatch, tmp_path):
    """Including the session doing the collecting -- it is anchored to its own tree."""
    _collect_setup(monkeypatch, in_use=True, age=workspace.GC_AGE_SECONDS + 1)
    assert workspace._collectable(tmp_path, {"feat/x"}) is None


def test_gc_reports_why_each_worktree_went(monkeypatch, tmp_path, capsys):
    spent = tmp_path / "session-spent"
    spent.mkdir()
    monkeypatch.setattr(workspace, "managed_repos", lambda: ["agent"])
    monkeypatch.setattr(workspace, "repo_path", lambda repo: tmp_path)
    monkeypatch.setattr(workspace.git, "is_checkout", lambda path: True)
    monkeypatch.setattr(workspace, "session_worktrees", lambda repo: [spent])
    monkeypatch.setattr(workspace, "merged_branches", lambda repo: {"feat/spent"})
    monkeypatch.setattr(workspace, "_collectable", lambda worktree, merged: "merged")
    monkeypatch.setattr(
        workspace.git, "run", lambda args, cwd=None, check=True: type("R", (), {"returncode": 0})()
    )
    monkeypatch.setattr(workspace, "_forget_session_pointers", lambda worktree: None)
    monkeypatch.setattr(workspace, "prune_session_pointers", lambda: 0)

    assert workspace.gc() == 1
    assert "gc: removed session-spent (merged)" in capsys.readouterr().out


def test_gc_asks_github_nothing_for_a_repo_with_no_worktrees(monkeypatch, tmp_path):
    """A quiet host must cost no API calls at all -- this runs at every session start."""
    calls = []
    monkeypatch.setattr(workspace, "managed_repos", lambda: ["agent"])
    monkeypatch.setattr(workspace, "repo_path", lambda repo: tmp_path)
    monkeypatch.setattr(workspace.git, "is_checkout", lambda path: True)
    monkeypatch.setattr(workspace, "session_worktrees", lambda repo: [])
    monkeypatch.setattr(workspace, "merged_branches", lambda repo: calls.append(repo) or set())
    monkeypatch.setattr(workspace, "prune_session_pointers", lambda: 0)

    assert workspace.gc() == 0
    assert calls == []
