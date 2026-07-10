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
