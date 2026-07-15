from __future__ import annotations

import pytest

from agentcli import github, issue_enable
from agentcli.errors import AgentHTTPError, AgentInputError


class _Resp:
    """Minimal stand-in for httpx.Response (status_code + json/text)."""

    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


_EXPECTED_CALLERS = {
    "issue-agent.yml",
    "issue-resume.yml",
    "pr-agent.yml",
    "agent-label.yml",
    "pr-review.yml",
}


def test_caller_workflows_pin_ref_and_leave_no_placeholder():
    files = issue_enable.caller_workflows("v1.2.3")
    assert set(files) == _EXPECTED_CALLERS
    for name, content in files.items():
        assert "{ref}" not in content, name
        assert "brujoand/agent/.github/workflows/" in content, name
        assert "@v1.2.3" in content, name


def test_ensure_installed_rejects_uninstalled(monkeypatch):
    monkeypatch.setattr(issue_enable, "installed_slugs", lambda: {"brujoand/gitops-homelab"})
    with pytest.raises(AgentInputError, match="not installed"):
        issue_enable.ensure_installed("brujoand/waiting-games")


def test_ensure_installed_accepts_installed(monkeypatch):
    monkeypatch.setattr(issue_enable, "installed_slugs", lambda: {"brujoand/waiting-games"})
    issue_enable.ensure_installed("brujoand/waiting-games")  # no raise


def test_create_label_created(monkeypatch):
    monkeypatch.setattr(github, "api_post", lambda path, body: _Resp(201))
    assert issue_enable.create_label("brujoand/x", issue_enable.LABELS[0]) == "created"


def test_create_label_already_exists_is_idempotent(monkeypatch):
    # 422 = the label is already there; enabling twice must not error.
    monkeypatch.setattr(github, "api_post", lambda path, body: _Resp(422))
    assert issue_enable.create_label("brujoand/x", issue_enable.LABELS[0]) == "exists"


def test_create_label_raises_on_other_error(monkeypatch):
    monkeypatch.setattr(github, "api_post", lambda path, body: _Resp(500, text="boom"))
    with pytest.raises(AgentHTTPError):
        issue_enable.create_label("brujoand/x", issue_enable.LABELS[0])


def test_run_refuses_when_not_installed(monkeypatch):
    monkeypatch.setattr(issue_enable, "installed_slugs", lambda: set())
    with pytest.raises(AgentInputError):
        issue_enable.run("brujoand/waiting-games")


def test_run_dry_run_writes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(issue_enable, "installed_slugs", lambda: {"brujoand/waiting-games"})
    # Any write attempt in dry-run is a bug.
    monkeypatch.setattr(github, "api_post", lambda *a, **k: pytest.fail("dry-run must not POST"))
    monkeypatch.setattr(github, "api_put", lambda *a, **k: pytest.fail("dry-run must not PUT"))

    code = issue_enable.run("brujoand/waiting-games", ref="main", apply=False)
    out = capsys.readouterr().out

    assert code == 0
    assert "dry-run" in out
    assert "would create" in out  # labels planned, not created
    # Every caller rendered into the plan, pinned at the ref.
    assert "issue-agent.yml" in out and "pr-review.yml" in out
    assert "@main" in out
    # The human-only checklist is always shown.
    assert "HUMAN-ONLY steps" in out
    assert "agent setup rulesets --repo brujoand/waiting-games" in out


def test_run_apply_creates_labels(monkeypatch, capsys):
    monkeypatch.setattr(issue_enable, "installed_slugs", lambda: {"brujoand/waiting-games"})
    posted: list[str] = []

    def fake_post(path, body):
        posted.append(body["name"])
        return _Resp(201)

    monkeypatch.setattr(github, "api_post", fake_post)

    code = issue_enable.run("brujoand/waiting-games", apply=True, open_pr=False)
    out = capsys.readouterr().out

    assert code == 0
    assert posted == ["agent", "agent-waiting"]
    assert "created" in out
    assert "applying" in out


def test_run_apply_open_pr_uses_pr_flow(monkeypatch, capsys):
    monkeypatch.setattr(issue_enable, "installed_slugs", lambda: {"brujoand/waiting-games"})
    monkeypatch.setattr(github, "api_post", lambda path, body: _Resp(201))
    called = {}

    def fake_open_pr(repo, files, branch="agent/enable-issue-agent"):
        called["repo"] = repo
        called["files"] = set(files)
        return "https://github.com/brujoand/waiting-games/pull/1"

    monkeypatch.setattr(issue_enable, "open_enable_pr", fake_open_pr)

    code = issue_enable.run("brujoand/waiting-games", apply=True, open_pr=True)
    out = capsys.readouterr().out

    assert code == 0
    assert called["repo"] == "brujoand/waiting-games"
    assert called["files"] == _EXPECTED_CALLERS
    assert "pull/1" in out
