from __future__ import annotations

from pathlib import Path

import pytest

from agentcli import rules
from agentcli.errors import AgentConfigError


@pytest.fixture
def trees(monkeypatch, tmp_path):
    """A fake rules tree and an empty Claude config dir, wired into the module."""
    src = tmp_path / "src" / "rules"
    cfg = tmp_path / "cfg"
    src.mkdir(parents=True)
    cfg.mkdir()
    monkeypatch.setattr(rules, "source_dir", lambda: src)
    monkeypatch.setattr(rules, "config_root", lambda: cfg)
    monkeypatch.setattr(rules, "memory_file", lambda: cfg / "CLAUDE.md")
    return src, cfg / "CLAUDE.md"


def _make_rule(src: Path, name: str) -> Path:
    p = src / f"{name}.md"
    p.write_text(f"# {name}\n")
    return p


def test_available_lists_markdown_in_import_order(trees):
    src, _ = trees
    _make_rule(src, "working-with-brujoand")
    _make_rule(src, "host-notes")
    (src / "notes.txt").write_text("not a rule")
    (src / "nested").mkdir()

    assert [p.stem for p in rules.available()] == ["host-notes", "working-with-brujoand"]


def test_install_creates_memory_file_with_block(trees):
    src, memory = trees
    _make_rule(src, "host-notes")

    outcome, path = rules.install()

    assert outcome == "created"
    assert path == memory
    text = memory.read_text()
    assert rules.START in text and rules.END in text
    assert f"@{src / 'host-notes.md'}" in text
    assert rules.status() == "ok"


def test_install_is_idempotent(trees):
    src, memory = trees
    _make_rule(src, "host-notes")
    rules.install()
    before = memory.read_text()

    assert rules.install() == ("ok", memory)
    assert memory.read_text() == before


def test_install_appends_to_a_hand_written_memory_file(trees):
    src, memory = trees
    _make_rule(src, "host-notes")
    memory.write_text("# My notes\n\nkeep me\n")

    outcome, _ = rules.install()

    text = memory.read_text()
    assert outcome == "added"
    assert text.startswith("# My notes\n\nkeep me\n")
    assert rules.START in text


def test_install_updates_a_stale_block_and_preserves_surrounding_text(trees):
    src, memory = trees
    _make_rule(src, "host-notes")
    rules.install()
    memory.write_text(f"before\n\n{memory.read_text().strip()}\n\nafter\n")
    _make_rule(src, "working-with-brujoand")

    assert rules.status() == "stale"
    outcome, _ = rules.install()

    text = memory.read_text()
    assert outcome == "updated"
    assert text.startswith("before\n")
    assert text.rstrip().endswith("after")
    assert "working-with-brujoand.md" in text
    assert rules.status() == "ok"


def test_a_dropped_rule_leaves_no_stale_import(trees):
    src, memory = trees
    _make_rule(src, "host-notes")
    gone = _make_rule(src, "obsolete")
    rules.install()
    gone.unlink()

    assert rules.status() == "stale"
    rules.install()
    assert "obsolete" not in memory.read_text()


def test_import_paths_are_home_relative_when_under_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert (
        rules._import_path(tmp_path / "src" / "agent" / "rules" / "a.md")
        == "~/src/agent/rules/a.md"
    )
    assert rules._import_path(Path("/opt/rules/a.md")) == "/opt/rules/a.md"


def test_unterminated_marker_is_an_error_not_a_silent_overwrite(trees):
    src, memory = trees
    _make_rule(src, "host-notes")
    memory.write_text(f"{rules.START}\n@somewhere.md\n")

    with pytest.raises(AgentConfigError, match="closing"):
        rules.install()
    assert "@somewhere.md" in memory.read_text()


def test_status_reports_absent_when_file_has_no_block(trees):
    src, memory = trees
    _make_rule(src, "host-notes")
    memory.write_text("# mine\n")

    assert rules.status() == "absent"


def test_install_fails_when_source_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(rules, "source_dir", lambda: tmp_path / "gone")
    with pytest.raises(AgentConfigError, match="agent pull"):
        rules.install()


def test_check_passes_but_nudges_when_not_installed(trees):
    src, _ = trees
    _make_rule(src, "host-notes")

    ok, detail = rules.check()

    assert ok is True
    assert "agent rules install" in detail


def test_check_fails_on_a_stale_block(trees):
    src, _ = trees
    _make_rule(src, "host-notes")
    rules.install()
    _make_rule(src, "second")

    ok, detail = rules.check()

    assert ok is False
    assert "out of date" in detail


def test_config_root_honors_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert rules.memory_file() == tmp_path / "cfg" / "CLAUDE.md"


def test_source_dir_is_agent_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SRC_ROOT", str(tmp_path))
    assert rules.source_dir() == tmp_path / "agent" / "rules"
