from __future__ import annotations

import json

import pytest

from agentcli import rulesets
from agentcli.errors import AgentAuthError, AgentError


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _fake_run(monkeypatch, handler):
    monkeypatch.setattr(rulesets.subprocess, "run", handler)


# --- the credential guard: the security-critical surface ---------------------


def test_require_human_token_accepts_a_human(monkeypatch):
    _fake_run(monkeypatch, lambda *a, **k: _Result(json.dumps({"login": "brujoand"})))
    assert rulesets.require_human_token() == "brujoand"


def test_require_human_token_refuses_an_app_installation_token(monkeypatch):
    """Regression, verified against the live API.

    An installation token gets 403 on GET /user -- it is not a user. `gh` prints
    that error JSON to *stdout* and exits non-zero. An earlier guard checked
    `type == "Bot"` (a response that never arrives) and only tested "is stdout
    non-empty", so it parsed the error body, found no type, and let the bot
    through with an empty login.
    """
    body = json.dumps({"message": "Resource not accessible by integration", "status": "403"})
    _fake_run(monkeypatch, lambda *a, **k: _Result(stdout=body, returncode=1))
    with pytest.raises(AgentAuthError, match="not a human user"):
        rulesets.require_human_token()


def test_require_human_token_refuses_error_body_on_stdout_with_zero_exit(monkeypatch):
    """Belt and braces: even a 0 exit must not pass if there is no login."""
    body = json.dumps({"message": "Resource not accessible by integration"})
    _fake_run(monkeypatch, lambda *a, **k: _Result(stdout=body, returncode=0))
    with pytest.raises(AgentAuthError, match="not a human user"):
        rulesets.require_human_token()


def test_require_human_token_fails_closed_on_garbage(monkeypatch):
    _fake_run(monkeypatch, lambda *a, **k: _Result(stdout="not json", returncode=0))
    with pytest.raises(AgentAuthError, match="not a human user"):
        rulesets.require_human_token()


def test_require_human_token_fails_closed_when_unauthenticated(monkeypatch):
    _fake_run(monkeypatch, lambda *a, **k: _Result(stdout="", returncode=1, stderr="bad creds"))
    with pytest.raises(AgentAuthError, match="not a human user"):
        rulesets.require_human_token()


# --- drift detection ---------------------------------------------------------


def test_drifts_ignores_server_populated_fields():
    desired = {"name": "x", "enforcement": "active"}
    live = {"name": "x", "enforcement": "active", "id": 1, "created_at": "...", "_links": {}}
    assert not rulesets._drifts(live, desired)


def test_drifts_ignores_unmanaged_bypass_actors():
    """We never declare bypass_actors, so their presence must not read as drift."""
    desired = {"name": "x", "enforcement": "active"}
    live = {"name": "x", "enforcement": "active", "bypass_actors": [{"actor_id": 5}]}
    assert not rulesets._drifts(live, desired)


def test_drifts_detects_a_changed_rule():
    desired = {"name": "x", "rules": [{"type": "deletion"}]}
    live = {"name": "x", "rules": []}
    assert rulesets._drifts(live, desired)


def test_drifts_ignores_server_defaults_nested_inside_rules():
    """Regression: GitHub echoes `required_reviewers: []` inside rule parameters.

    A shallow comparison treats `rules` as one opaque value, so this nested
    server default made every repo report drift forever and `--apply` rewrite
    the entire fleet on every run.
    """
    desired = {
        "rules": [{"type": "pull_request", "parameters": {"require_last_push_approval": False}}]
    }
    live = {
        "rules": [
            {
                "type": "pull_request",
                "parameters": {"require_last_push_approval": False, "required_reviewers": []},
            }
        ]
    }
    assert not rulesets._drifts(live, desired)


def test_drifts_detects_a_changed_nested_parameter():
    desired = {
        "rules": [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
    }
    live = {
        "rules": [{"type": "pull_request", "parameters": {"required_approving_review_count": 0}}]
    }
    assert rulesets._drifts(live, desired)


def test_drifts_detects_reordered_or_missing_rules():
    desired = {"rules": [{"type": "deletion"}, {"type": "non_fast_forward"}]}
    assert rulesets._drifts(
        {"rules": [{"type": "non_fast_forward"}, {"type": "deletion"}]}, desired
    )
    assert rulesets._drifts({"rules": [{"type": "deletion"}]}, desired)


# --- apply_to: create / update / unchanged, and dry-run never writes ----------


def _stub_api(monkeypatch, *, existing, detail=None, writes=None):
    def handler(argv, **kwargs):
        if argv[:2] == ["gh", "api"] and "--method" in argv:
            writes.append(argv[argv.index("--method") + 1])
            return _Result(json.dumps({"updated_at": "after"}))
        path = argv[2]
        if path.endswith("/rulesets"):
            return _Result(json.dumps(existing))
        return _Result(json.dumps(detail or {}))

    _fake_run(monkeypatch, handler)


def test_apply_to_creates_when_absent(monkeypatch):
    writes: list[str] = []
    _stub_api(monkeypatch, existing=[], writes=writes)
    assert rulesets.apply_to("o/r", {"name": "x"}, dry_run=False) == "created"
    assert writes == ["POST"]


def test_apply_to_updates_when_drifted(monkeypatch):
    writes: list[str] = []
    _stub_api(
        monkeypatch,
        existing=[{"name": "x", "id": 7}],
        detail={"name": "x", "enforcement": "disabled"},
        writes=writes,
    )
    desired = {"name": "x", "enforcement": "active"}
    assert rulesets.apply_to("o/r", desired, dry_run=False) == "updated"
    assert writes == ["PUT"]


def test_apply_to_reports_unchanged_without_writing(monkeypatch):
    writes: list[str] = []
    _stub_api(
        monkeypatch,
        existing=[{"name": "x", "id": 7}],
        detail={"name": "x", "enforcement": "active"},
        writes=writes,
    )
    desired = {"name": "x", "enforcement": "active"}
    assert rulesets.apply_to("o/r", desired, dry_run=False) == "unchanged"
    assert writes == []


@pytest.mark.parametrize(
    ("existing", "detail", "expected"),
    [
        ([], None, "would create"),
        ([{"name": "x", "id": 7}], {"name": "x", "enforcement": "disabled"}, "would update"),
        ([{"name": "x", "id": 7}], {"name": "x", "enforcement": "active"}, "unchanged"),
    ],
)
def test_dry_run_never_writes(monkeypatch, existing, detail, expected):
    writes: list[str] = []
    _stub_api(monkeypatch, existing=existing, detail=detail, writes=writes)
    desired = {"name": "x", "enforcement": "active"}
    assert rulesets.apply_to("o/r", desired, dry_run=True) == expected
    assert writes == []


# --- the shipped definition --------------------------------------------------


def test_shipped_ruleset_loads_and_omits_bypass_actors():
    """bypass_actors are per-account ids that do not port across repos; declaring
    them would risk silently granting the agent a bypass."""
    desired = rulesets.load("protect-main-pr-only")
    assert desired["name"] == "protect-main-pr-only"
    assert "bypass_actors" not in desired
    assert {r["type"] for r in desired["rules"]} == {"deletion", "non_fast_forward", "pull_request"}


def test_load_rejects_unknown_ruleset():
    with pytest.raises(AgentError, match="no such ruleset"):
        rulesets.load("does-not-exist")


# --- fleet: the agent decides scope, and there is no silent fallback ---------


def test_fleet_uses_app_token_when_creds_present(monkeypatch):
    monkeypatch.setattr(
        rulesets.repos, "clone_urls", lambda: ["https://github.com/brujoand/agent.git"]
    )
    slugs, source = rulesets.fleet()
    assert slugs == ["brujoand/agent"]
    assert "App token" in source


def test_fleet_asks_the_agent_user_when_creds_absent(monkeypatch):
    """The human holds no App creds, so the fleet comes from `agent repos`."""

    def no_creds():
        raise AgentError("missing App credentials")

    monkeypatch.setattr(rulesets.repos, "clone_urls", no_creds)
    monkeypatch.setattr(
        rulesets.subprocess,
        "run",
        lambda *a, **k: _Result(
            "https://github.com/brujoand/gitops-homelab.git\nhttps://github.com/brujoand/agent.git\n"
        ),
    )
    slugs, source = rulesets.fleet()
    assert slugs == ["brujoand/agent", "brujoand/gitops-homelab"]
    assert "agent repos" in source


def test_fleet_never_falls_back_to_repos_you_own(monkeypatch):
    """Regression: falling back to `gh repo list` substituted 62 owned repos for
    the 11 the App reaches, so --apply would protect 51 unrelated repos."""

    def no_creds():
        raise AgentError("missing App credentials")

    monkeypatch.setattr(rulesets.repos, "clone_urls", no_creds)
    monkeypatch.setattr(
        rulesets.subprocess,
        "run",
        lambda *a, **k: _Result(stdout="", returncode=1, stderr="sudo: a password is required"),
    )
    with pytest.raises(AgentAuthError, match="could not ask"):
        rulesets.fleet()
