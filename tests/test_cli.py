"""Smoke tests that actually invoke commands.

The gap these close: a command body referencing a module the import block never
imported is a `NameError` at *invocation*, not at import. Every unit test can pass
while `agent settings list` dies on its first line, because nothing imported
`cli`. `--help` does not catch it either -- Typer renders help from the signature
without running the body. Only invoking does.

Read-only commands only. Anything that reaches the network, mints a token, or
writes outside a tmp_path belongs in its own module's tests with fakes.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from agentcli.cli import app

runner = CliRunner()


def _isolated(monkeypatch, tmp_path):
    """Point the CLI at an empty checkout root and config dir."""
    src = tmp_path / "src"
    (src / "agent" / "settings").mkdir(parents=True)
    config = tmp_path / "cfg"
    config.mkdir()
    monkeypatch.setenv("AGENT_SRC_ROOT", str(src))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return src / "agent", config


def test_help_lists_every_sub_app():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in (
        "github",
        "workspace",
        "skills",
        "rules",
        "hooks",
        "settings",
        "output-styles",
        "usage",
        "doctor",
    ):
        assert name in result.stdout


def test_output_styles_list_runs(monkeypatch, tmp_path):
    repo, _ = _isolated(monkeypatch, tmp_path)
    (repo / "output-styles").mkdir(parents=True)
    (repo / "output-styles" / "terse.md").write_text("---\nname: terse\n---\n\nbody\n")
    result = runner.invoke(app, ["output-styles", "list"])
    assert result.exit_code == 0
    assert "terse.md" in result.stdout
    assert "missing" in result.stdout


def test_output_styles_install_links(monkeypatch, tmp_path):
    repo, config = _isolated(monkeypatch, tmp_path)
    (repo / "output-styles").mkdir(parents=True)
    (repo / "output-styles" / "terse.md").write_text("---\nname: terse\n---\n\nbody\n")
    result = runner.invoke(app, ["output-styles", "install"])
    assert result.exit_code == 0
    assert (config / "output-styles" / "terse.md").is_symlink()


def test_settings_list_runs(monkeypatch, tmp_path):
    repo, _ = _isolated(monkeypatch, tmp_path)
    (repo / "settings" / "settings.json").write_text(json.dumps({"effortLevel": "high"}))

    result = runner.invoke(app, ["settings", "list"])

    assert result.exit_code == 0, result.output
    assert "effortLevel" in result.stdout


def test_settings_install_runs_and_is_idempotent(monkeypatch, tmp_path):
    repo, config = _isolated(monkeypatch, tmp_path)
    (repo / "settings" / "settings.json").write_text(json.dumps({"effortLevel": "high"}))

    first = runner.invoke(app, ["settings", "install"])
    second = runner.invoke(app, ["settings", "install"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "set" in first.stdout
    assert "ok" in second.stdout
    assert json.loads((config / "settings.json").read_text())["effortLevel"] == "high"


def test_settings_list_reports_a_missing_declaration_without_a_traceback(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)

    result = runner.invoke(app, ["settings", "list"])

    # An unfetched checkout is an expected state, not a crash.
    assert result.exit_code == 1
    assert "agent pull" in result.stdout
    assert "Traceback" not in result.stdout


def test_usage_runs_on_an_empty_transcript_root(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    result = runner.invoke(app, ["usage", "--days", "1"])
    assert result.exit_code == 0, result.output
    assert "no billed turns" in result.stdout
