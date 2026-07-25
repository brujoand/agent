from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentcli import hooks
from agentcli.errors import AgentConfigError

DECLARATION = {
    "UserPromptSubmit": [
        {"hooks": [{"type": "command", "script": "tmux-title.sh", "timeout": 5, "async": True}]}
    ],
    "PostToolUse": [
        {
            "matcher": "ExitPlanMode",
            "hooks": [{"type": "command", "script": "tmux-title.sh", "timeout": 5}],
        }
    ],
}


def _make_hook(root: Path, name: str) -> Path:
    script = root / name
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    script.chmod(0o755)
    return script


@pytest.fixture
def trees(monkeypatch, tmp_path):
    """A fake source tree, an empty dest, and a settings file path, wired in."""
    src = tmp_path / "src" / "hooks"
    config = tmp_path / "dest"
    src.mkdir(parents=True)
    config.mkdir(parents=True)
    monkeypatch.setattr(hooks, "source_dir", lambda: src)
    monkeypatch.setattr(hooks, "config_root", lambda: config)
    monkeypatch.setattr(hooks, "dest_dir", lambda: config / "hooks")
    monkeypatch.setattr(hooks, "settings_file", lambda: config / "settings.json")
    return src, config / "hooks", config / "settings.json"


def _declare(src: Path, data: dict | None = None) -> None:
    (src / "hooks.json").write_text(json.dumps(data if data is not None else DECLARATION))


def _settings(path: Path) -> dict:
    return json.loads(path.read_text())


def test_available_lists_only_scripts_not_the_declaration(trees):
    src, _, _ = trees
    _make_hook(src, "tmux-title.sh")
    _declare(src)

    assert [p.name for p in hooks.available()] == ["tmux-title.sh"]


def test_install_symlinks_and_wires_both_events(trees):
    src, dest, settings = trees
    _make_hook(src, "tmux-title.sh")
    _declare(src)

    results, outcome, path = hooks.install()

    assert results == [("tmux-title.sh", "linked")]
    assert outcome == "created"
    assert path == settings
    assert (dest / "tmux-title.sh").resolve() == (src / "tmux-title.sh").resolve()

    wired = _settings(settings)["hooks"]
    assert wired["UserPromptSubmit"][0]["hooks"][0]["command"] == str(dest / "tmux-title.sh")
    assert wired["UserPromptSubmit"][0]["hooks"][0]["async"] is True
    assert wired["PostToolUse"][0]["matcher"] == "ExitPlanMode"


def test_install_is_idempotent(trees):
    src, _, _ = trees
    _make_hook(src, "tmux-title.sh")
    _declare(src)
    hooks.install()

    results, outcome, _ = hooks.install()

    assert results == [("tmux-title.sh", "ok")]
    assert outcome == "ok"
    assert hooks.settings_status() == "ok"


def test_install_preserves_unrelated_settings_and_foreign_hooks(trees):
    src, dest, settings = trees
    _make_hook(src, "tmux-title.sh")
    _declare(src)
    settings.write_text(
        json.dumps(
            {
                "theme": "auto",
                "permissions": {"deny": ["Bash(rm -rf /*)"]},
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Edit",
                            "hooks": [{"type": "command", "command": "~/.claude/hooks/guard.sh"}],
                        }
                    ]
                },
            }
        )
    )

    hooks.install()

    after = _settings(settings)
    assert after["theme"] == "auto"
    assert after["permissions"]["deny"] == ["Bash(rm -rf /*)"]
    guard = after["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert guard == "~/.claude/hooks/guard.sh"
    assert "UserPromptSubmit" in after["hooks"]


def test_install_joins_an_existing_group_with_the_same_matcher(trees):
    src, _, settings = trees
    _make_hook(src, "tmux-title.sh")
    _declare(src)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "ExitPlanMode",
                            "hooks": [{"type": "command", "command": "/usr/local/bin/mine.sh"}],
                        }
                    ]
                }
            }
        )
    )

    hooks.install()

    groups = _settings(settings)["hooks"]["PostToolUse"]
    assert len(groups) == 1
    commands = [e["command"] for e in groups[0]["hooks"]]
    assert commands[0] == "/usr/local/bin/mine.sh"
    assert commands[1].endswith("tmux-title.sh")


def test_rewiring_moves_the_entry_instead_of_duplicating_it(trees):
    src, _, settings = trees
    _make_hook(src, "tmux-title.sh")
    _declare(src)
    hooks.install()

    _declare(src, {"SessionStart": [{"hooks": [{"type": "command", "script": "tmux-title.sh"}]}]})
    _, outcome, _ = hooks.install()

    after = _settings(settings)["hooks"]
    assert outcome == "updated"
    assert list(after) == ["SessionStart"]
    assert after["SessionStart"][0]["hooks"][0]["command"].endswith("tmux-title.sh")


def test_install_prunes_a_hook_that_left_the_source_tree(trees):
    src, dest, settings = trees
    script = _make_hook(src, "tmux-title.sh")
    _declare(src)
    hooks.install()

    script.unlink()
    _declare(src, {})
    results, _, _ = hooks.install()

    assert results == [("tmux-title.sh", "pruned")]
    assert not (dest / "tmux-title.sh").is_symlink()
    assert "hooks" not in _settings(settings)


def test_install_never_clobbers_a_hand_placed_hook(trees):
    src, dest, _ = trees
    _make_hook(src, "tmux-title.sh")
    _declare(src)
    dest.mkdir(parents=True)
    (dest / "tmux-title.sh").write_text("mine")

    results, _, _ = hooks.install()

    assert "SKIP" in results[0][1]
    assert (dest / "tmux-title.sh").read_text() == "mine"


def test_install_leaves_a_users_own_dangling_link_alone(trees):
    src, dest, _ = trees
    _make_hook(src, "tmux-title.sh")
    _declare(src)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "mine.sh").symlink_to(Path("/nowhere/mine.sh"))

    hooks.install()

    assert (dest / "mine.sh").is_symlink()


def test_broken_settings_json_is_never_clobbered(trees):
    src, _, settings = trees
    _make_hook(src, "tmux-title.sh")
    _declare(src)
    settings.write_text("{ this is not json")

    with pytest.raises(AgentConfigError):
        hooks.install()
    assert settings.read_text() == "{ this is not json"


def test_install_without_source_raises(trees, monkeypatch, tmp_path):
    monkeypatch.setattr(hooks, "source_dir", lambda: tmp_path / "gone" / "hooks")
    with pytest.raises(AgentConfigError):
        hooks.install()


def test_check_reports_unwired_without_failing(trees):
    src, _, _ = trees
    _make_hook(src, "tmux-title.sh")
    _declare(src)

    ok, detail = hooks.check()

    assert ok is True
    assert "wiring missing" in detail


def test_check_ok_after_install(trees):
    src, _, _ = trees
    _make_hook(src, "tmux-title.sh")
    _declare(src)
    hooks.install()

    ok, detail = hooks.check()

    assert ok is True
    assert "1 linked" in detail


def test_check_fails_on_conflict(trees):
    src, dest, _ = trees
    _make_hook(src, "tmux-title.sh")
    _declare(src)
    dest.mkdir(parents=True)
    (dest / "tmux-title.sh").write_text("mine")

    ok, detail = hooks.check()

    assert ok is False
    assert "conflict" in detail


def test_dest_dir_honors_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert hooks.dest_dir() == tmp_path / "cfg" / "hooks"
    assert hooks.settings_file() == tmp_path / "cfg" / "settings.json"


def test_source_dir_is_agent_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SRC_ROOT", str(tmp_path))
    assert hooks.source_dir() == tmp_path / "agent" / "hooks"


# --- the shipped script's own behaviour ---------------------------------------

SCRIPT = Path(__file__).resolve().parent.parent / "hooks" / "tmux-title.sh"


def _title(payload: dict) -> str:
    done = subprocess.run(
        [str(SCRIPT), "--print"], input=json.dumps(payload), capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("fix the failing pre-commit check", "fixFailing"),
        ("Why is the build broken again?", "buildBrokenAgain"),
        ("", ""),
        ("https://example.com/pull/42 please review this", "review"),
        ("supercalifragilisticexpialidocious", "supercalifragilist"),
    ],
)
def test_script_titles_a_prompt(prompt, expected):
    assert _title({"session_id": "t", "prompt": prompt}) == expected


def test_script_skips_generic_plan_headings():
    plan = "## Goal\nAdd a tmux window title hook\n\n1. Write the script"
    assert _title({"session_id": "t", "tool_input": {"plan": plan}}) == "addTmuxWindow"


def test_script_title_never_exceeds_the_tab_budget():
    long_prompt = "investigate the recurring storage daemon crash on the third worker node"
    assert len(_title({"session_id": "t", "prompt": long_prompt})) <= 18
