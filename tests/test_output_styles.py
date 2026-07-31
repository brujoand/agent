"""Output-styles tree: link state, install outcomes, pruning, doctor probe.

Same shape as test_skills.py -- the tree is `skills.py` over files instead of
directories -- with the two differences that matter tested explicitly: only
`.md` files are styles, and README.md in the tree is prose, not a style.
"""

import json
from pathlib import Path

import pytest

from agentcli import output_styles
from agentcli.errors import AgentConfigError

REPO = Path(__file__).resolve().parents[1]

STYLE = "---\nname: t\n---\n\nbody\n"


def _make_style(root, name, body=STYLE):
    path = root / name
    path.write_text(body)
    return path


@pytest.fixture
def trees(monkeypatch, tmp_path):
    """A fake source tree and an empty dest, wired into the module."""
    src = tmp_path / "src" / "output-styles"
    dest = tmp_path / "dest" / "output-styles"
    src.mkdir(parents=True)
    monkeypatch.setattr(output_styles, "source_dir", lambda: src)
    monkeypatch.setattr(output_styles, "dest_dir", lambda: dest)
    return src, dest


def test_available_takes_markdown_only(trees):
    src, _ = trees
    _make_style(src, "terse.md")
    _make_style(src, "notes.txt")
    (src / "nested").mkdir()
    assert [p.name for p in output_styles.available()] == ["terse.md"]


def test_available_ignores_the_tree_readme(trees):
    src, _ = trees
    _make_style(src, "terse.md")
    _make_style(src, "README.md", "# prose about the tree\n")
    assert [p.name for p in output_styles.available()] == ["terse.md"]


def test_install_links_and_creates_dest(trees):
    src, dest = trees
    _make_style(src, "terse.md")
    assert output_styles.install() == [("terse.md", "linked")]
    assert (dest / "terse.md").is_symlink()
    assert (dest / "terse.md").read_text() == STYLE


def test_install_is_idempotent(trees):
    src, _ = trees
    _make_style(src, "terse.md")
    output_styles.install()
    assert output_styles.install() == [("terse.md", "ok")]


def test_install_relinks_a_stale_link(trees):
    src, dest = trees
    _make_style(src, "terse.md")
    dest.mkdir(parents=True)
    elsewhere = src.parent / "elsewhere.md"
    elsewhere.write_text("other\n")
    (dest / "terse.md").symlink_to(elsewhere)

    assert output_styles.status("terse.md") == "stale"
    assert output_styles.install() == [("terse.md", "relinked")]
    assert (dest / "terse.md").resolve() == (src / "terse.md").resolve()


def test_install_never_clobbers_a_hand_written_style(trees):
    src, dest = trees
    _make_style(src, "terse.md")
    dest.mkdir(parents=True)
    (dest / "terse.md").write_text("mine\n")

    results = output_styles.install()
    assert results[0][0] == "terse.md"
    assert results[0][1].startswith("SKIP")
    assert (dest / "terse.md").read_text() == "mine\n"


def test_install_prunes_a_departed_style(trees):
    src, dest = trees
    _make_style(src, "gone.md")
    output_styles.install()
    (src / "gone.md").unlink()

    assert output_styles.install() == [("gone.md", "pruned")]
    assert not (dest / "gone.md").is_symlink()


def test_install_leaves_a_foreign_dangling_link_alone(trees):
    src, dest = trees
    _make_style(src, "terse.md")
    dest.mkdir(parents=True)
    (dest / "theirs.md").symlink_to(src.parent / "never-existed.md")

    output_styles.install()
    assert (dest / "theirs.md").is_symlink()


def test_install_without_a_source_tree_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(output_styles, "source_dir", lambda: tmp_path / "nope")
    with pytest.raises(AgentConfigError, match="agent pull"):
        output_styles.install()


def test_check_before_install_is_ok_with_a_hint(trees):
    src, _ = trees
    _make_style(src, "terse.md")
    ok, detail = output_styles.check()
    assert ok
    assert "0/1 linked" in detail
    assert "agent output-styles install" in detail


def test_check_after_install_is_clean(trees):
    src, _ = trees
    _make_style(src, "terse.md")
    output_styles.install()
    ok, detail = output_styles.check()
    assert ok
    assert "1 linked" in detail


def test_check_fails_on_a_conflict(trees):
    src, dest = trees
    _make_style(src, "terse.md")
    dest.mkdir(parents=True)
    (dest / "terse.md").write_text("mine\n")

    ok, detail = output_styles.check()
    assert not ok
    assert "conflict" in detail


def test_check_without_a_source_tree_points_at_pull(monkeypatch, tmp_path):
    monkeypatch.setattr(output_styles, "source_dir", lambda: tmp_path / "nope")
    ok, detail = output_styles.check()
    assert not ok
    assert "agent pull" in detail


def test_dest_dir_honors_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert output_styles.dest_dir() == tmp_path / "cfg" / "output-styles"


def test_source_dir_is_agent_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SRC_ROOT", str(tmp_path))
    assert output_styles.source_dir() == tmp_path / "agent" / "output-styles"


# The two halves are declared in different files, so nothing but a test stops
# them drifting: settings.json names the active style, this tree has to carry it,
# and Claude Code matches on the frontmatter `name` (which defaults to the
# filename) -- so all three have to agree.
def test_the_declared_style_ships_in_the_tree():
    declared = json.loads((REPO / "settings" / "settings.json").read_text())["outputStyle"]
    path = REPO / "output-styles" / f"{declared}.md"
    assert path.is_file(), f"settings.json selects {declared!r} but {path} does not exist"
    assert f"\nname: {declared}\n" in path.read_text()
