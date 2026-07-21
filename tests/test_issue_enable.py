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


# Issues are handled by the central hub, so `enable` lays down only the per-repo
# PR-review caller (+ labels + the pre-commit bundle).
_EXPECTED_CALLERS = {
    ".github/workflows/pr-review.yml",
}
_EXPECTED_BUNDLE = {
    ".pre-commit-config.yaml",
    ".github/workflows/pre-commit.yml",
}


def test_caller_workflows_render_pr_review_only():
    files = issue_enable.caller_workflows("v1.2.3", "myorg/agent", "my-agent")
    assert set(files) == _EXPECTED_CALLERS  # keyed by repo path; PR review only
    content = files[".github/workflows/pr-review.yml"]
    # No template token survives rendering.
    assert "{ref}" not in content
    assert "{reusable_repo}" not in content
    # Pinned at the given reusable repo + ref.
    assert "myorg/agent/.github/workflows/pr-review.reusable.yml@v1.2.3" in content


def test_bundle_files_carry_baseline_and_denylist():
    files = issue_enable.bundle_files()
    assert set(files) == _EXPECTED_BUNDLE
    cfg = files[".pre-commit-config.yaml"]
    assert "gitleaks" in cfg
    assert "no-internal-infra" in cfg
    assert "{denylist}" not in cfg  # rendered
    # Generic infra patterns are present (as escaped regex); no private domain
    # is hardcoded into this public repo.
    assert r"svc\.cluster\.local" in cfg
    assert "brujordet" not in cfg


def test_denylist_appends_extra_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_DENYLIST_EXTRA", r"example\.internal")
    assert r"example\.internal" in issue_enable._denylist_regex()
    monkeypatch.delenv("AGENT_DENYLIST_EXTRA", raising=False)
    assert "example" not in issue_enable._denylist_regex()


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
    monkeypatch.setattr(github, "app_slug", lambda: "my-agent")
    # Any write attempt in dry-run is a bug.
    monkeypatch.setattr(github, "api_post", lambda *a, **k: pytest.fail("dry-run must not POST"))
    monkeypatch.setattr(github, "api_put", lambda *a, **k: pytest.fail("dry-run must not PUT"))

    code = issue_enable.run("brujoand/waiting-games", ref="main", apply=False)
    out = capsys.readouterr().out

    assert code == 0
    assert "dry-run" in out
    assert "would create" in out  # labels planned, not created
    # Only the per-repo PR-review caller is rendered (issues are central).
    assert "pr-review.yml" in out
    assert "issue-agent.yml" not in out
    assert "@main" in out
    # The standard bundle (pre-commit + the internal-infra denylist) is included.
    assert ".pre-commit-config.yaml" in out
    assert "no-internal-infra" in out
    # The human-only checklist is always shown.
    assert "HUMAN-ONLY steps" in out
    assert "agent setup rulesets --repo brujoand/waiting-games" in out


def test_run_honours_reusable_repo_override(monkeypatch, capsys):
    monkeypatch.setattr(issue_enable, "installed_slugs", lambda: {"brujoand/waiting-games"})
    monkeypatch.setattr(github, "app_slug", lambda: "my-agent")

    issue_enable.run("brujoand/waiting-games", reusable_repo="myorg/agent", apply=False)
    out = capsys.readouterr().out
    assert "myorg/agent/.github/workflows/" in out
    assert "brujoand/agent/.github/workflows/" not in out


def test_run_apply_creates_labels(monkeypatch, capsys):
    monkeypatch.setattr(issue_enable, "installed_slugs", lambda: {"brujoand/waiting-games"})
    monkeypatch.setattr(github, "app_slug", lambda: "my-agent")
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
    monkeypatch.setattr(github, "app_slug", lambda: "my-agent")
    monkeypatch.setattr(github, "api_post", lambda path, body: _Resp(201))
    called = {}

    def fake_open_pr(repo, files, reusable_repo, no_clobber=frozenset(), branch="x"):
        called["repo"] = repo
        called["files"] = set(files)
        called["reusable_repo"] = reusable_repo
        called["no_clobber"] = set(no_clobber)
        return "https://github.com/brujoand/waiting-games/pull/1"

    monkeypatch.setattr(issue_enable, "open_enable_pr", fake_open_pr)

    code = issue_enable.run("brujoand/waiting-games", apply=True, open_pr=True)
    out = capsys.readouterr().out

    assert code == 0
    assert called["repo"] == "brujoand/waiting-games"
    # Callers + the standard bundle are all offered; the bundle is no-clobber.
    assert called["files"] == _EXPECTED_CALLERS | _EXPECTED_BUNDLE
    assert called["no_clobber"] == _EXPECTED_BUNDLE
    assert called["reusable_repo"] == "brujoand/agent"
    assert "pull/1" in out
