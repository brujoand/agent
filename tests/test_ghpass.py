from __future__ import annotations

import pytest

from agentcli import ghpass
from agentcli.errors import AgentError


@pytest.fixture
def captured_exec(monkeypatch):
    """Capture os.execve so exec_gh never actually replaces the test process."""
    captured: dict[str, object] = {}

    def fake_execve(path, argv, env):
        captured["path"] = path
        captured["argv"] = argv
        captured["env"] = env

    monkeypatch.setattr(ghpass.os, "execve", fake_execve)
    return captured


def test_exec_gh_injects_token_and_execs(monkeypatch, captured_exec):
    monkeypatch.setattr(ghpass.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(ghpass.github, "token", lambda: "tok-123")

    ghpass.exec_gh(["pr", "view", "1337", "--json", "title"])

    assert captured_exec["path"] == "/usr/bin/gh"
    # gh, not agent, owns the argv: the passthrough args reach it verbatim.
    assert captured_exec["argv"] == ["gh", "pr", "view", "1337", "--json", "title"]
    assert captured_exec["env"]["GH_TOKEN"] == "tok-123"


def test_exec_gh_overrides_existing_gh_token(monkeypatch, captured_exec):
    monkeypatch.setattr(ghpass.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(ghpass.github, "token", lambda: "fresh")
    monkeypatch.setenv("GH_TOKEN", "stale")

    ghpass.exec_gh(["repo", "view"])

    assert captured_exec["env"]["GH_TOKEN"] == "fresh"


def test_exec_gh_missing_binary_raises_without_minting(monkeypatch, captured_exec):
    monkeypatch.setattr(ghpass.shutil, "which", lambda name: None)

    def fail_token():
        raise AssertionError("token minted despite gh being absent")

    monkeypatch.setattr(ghpass.github, "token", fail_token)

    with pytest.raises(AgentError):
        ghpass.exec_gh(["pr", "view"])

    assert captured_exec == {}
