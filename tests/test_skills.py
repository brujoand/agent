from __future__ import annotations

from pathlib import Path

import pytest

from agentcli import skills
from agentcli.errors import AgentConfigError


def _make_skill(root: Path, name: str, with_md: bool = True) -> Path:
    d = root / name
    d.mkdir(parents=True)
    if with_md:
        (d / "SKILL.md").write_text("---\nname: x\n---\nbody\n")
    return d


@pytest.fixture
def trees(monkeypatch, tmp_path):
    """A fake source tree and an empty dest, wired into the module."""
    src = tmp_path / "src" / "skills"
    dest = tmp_path / "dest" / "skills"
    src.mkdir(parents=True)
    monkeypatch.setattr(skills, "source_dir", lambda: src)
    monkeypatch.setattr(skills, "dest_dir", lambda: dest)
    return src, dest


def test_available_lists_only_dirs_with_skill_md(trees):
    src, _ = trees
    _make_skill(src, "working-with-brujoand")
    _make_skill(src, "half-baked", with_md=False)
    (src / "loose.md").write_text("not a skill")

    assert [d.name for d in skills.available()] == ["working-with-brujoand"]


def test_install_symlinks_into_dest_and_reports_linked(trees):
    src, dest = trees
    _make_skill(src, "working-with-brujoand")

    results = skills.install()

    assert results == [("working-with-brujoand", "linked")]
    link = dest / "working-with-brujoand"
    assert link.is_symlink()
    assert link.resolve() == (src / "working-with-brujoand").resolve()
    assert skills.status("working-with-brujoand") == "ok"


def test_install_is_idempotent(trees):
    src, _ = trees
    _make_skill(src, "a")
    skills.install()

    assert skills.install() == [("a", "ok")]


def test_install_relinks_a_stale_symlink(trees):
    src, dest = trees
    _make_skill(src, "a")
    dest.mkdir(parents=True)
    (dest / "a").symlink_to(src / "nonexistent-old-target")

    assert skills.status("a") == "stale"
    assert skills.install() == [("a", "relinked")]
    assert skills.status("a") == "ok"


def test_install_never_clobbers_a_real_dir(trees):
    src, dest = trees
    _make_skill(src, "a")
    dest.mkdir(parents=True)
    hand_made = dest / "a"
    hand_made.mkdir()
    (hand_made / "SKILL.md").write_text("mine")

    results = skills.install()

    assert results[0][0] == "a"
    assert "SKIP" in results[0][1]
    assert not (dest / "a").is_symlink()
    assert (dest / "a" / "SKILL.md").read_text() == "mine"


def test_install_without_source_raises(trees, monkeypatch, tmp_path):
    monkeypatch.setattr(skills, "source_dir", lambda: tmp_path / "gone" / "skills")
    with pytest.raises(AgentConfigError):
        skills.install()


def test_check_reports_partial_install_without_failing(trees):
    src, _ = trees
    _make_skill(src, "a")

    ok, detail = skills.check()

    assert ok is True
    assert "0/1 linked" in detail


def test_check_ok_after_install(trees):
    src, _ = trees
    _make_skill(src, "a")
    skills.install()

    ok, detail = skills.check()

    assert ok is True
    assert "1 linked" in detail


def test_check_fails_on_conflict(trees):
    src, dest = trees
    _make_skill(src, "a")
    dest.mkdir(parents=True)
    (dest / "a").mkdir()

    ok, detail = skills.check()

    assert ok is False
    assert "conflict" in detail


def test_check_fails_when_source_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(skills, "source_dir", lambda: tmp_path / "gone")
    ok, detail = skills.check()
    assert ok is False
    assert "agent pull" in detail


def test_dest_dir_honors_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert skills.dest_dir() == tmp_path / "cfg" / "skills"


def test_source_dir_is_agent_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SRC_ROOT", str(tmp_path))
    assert skills.source_dir() == tmp_path / "agent" / "skills"
