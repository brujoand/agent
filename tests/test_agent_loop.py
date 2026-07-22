"""The wrapper loop above the provider seam: markers, usage totals, and one
thin end-to-end pass of run() driven by a fake provider."""

import agent
import anyio
import pytest
from providers.base import SessionConfig, TurnResult, TurnUsage


def test_ask_marker_extraction():
    text = "preamble\n<<<ASK>>>\n1. Which env?\n2. Which branch?\n<<<END_ASK>>>\ntrailer"
    m = agent.ASK_RE.search(text)
    assert m is not None
    assert m.group(1).strip() == "1. Which env?\n2. Which branch?"


def test_done_marker_paired_and_bare():
    paired = "work done\n<<<DONE>>>\nOpened #12.\n<<<END_DONE>>>"
    assert agent.DONE_MARKER in paired
    assert agent.DONE_RE.search(paired).group(1).strip() == "Opened #12."


def test_run_record_formats_status_and_source(monkeypatch):
    monkeypatch.setenv("TRIGGER_SOURCE", "autopilot-schedule")
    line = agent.run_record("paused")
    assert "Run paused" in line
    assert "autopilot-schedule" in line


def test_run_record_defaults_source_to_human(monkeypatch):
    monkeypatch.delenv("TRIGGER_SOURCE", raising=False)
    assert "human" in agent.run_record("completed")

    bare = "work done\n<<<DONE>>>"
    assert agent.DONE_MARKER in bare
    assert agent.DONE_RE.search(bare) is None  # falls back to footer-only comment


def test_usage_tracker_accumulates_turn_usage(monkeypatch):
    monkeypatch.setattr(agent.UsageTracker, "push", lambda self: None)
    tracker = agent.UsageTracker(issue="7", model="m")
    tracker.record(TurnUsage(input_tokens=10, output_tokens=5, cost_usd=0.1, num_turns=2))
    tracker.record(
        TurnUsage(
            input_tokens=1,
            cache_creation_input_tokens=3,
            cache_read_input_tokens=4,
            cost_usd=0.05,
            num_turns=1,
        )
    )
    assert tracker.tokens["input_tokens"] == 11
    assert tracker.tokens["output_tokens"] == 5
    assert tracker.tokens["cache_creation_input_tokens"] == 3
    assert tracker.tokens["cache_read_input_tokens"] == 4
    assert tracker.cost_usd == pytest.approx(0.15)
    assert tracker.num_turns == 3


class FakeSession:
    """AgentSession that replays scripted TurnResults."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.prompts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def run_turn(self, prompt):
        self.prompts.append(prompt)
        return self.turns.pop(0)


class FakeProvider:
    def __init__(self, session):
        self.session = session
        self.config = None

    async def session_exists(self, session_id, cwd):
        return False

    def open_session(self, config):
        self.config = config
        return self.session


def run_loop(monkeypatch, session, comments, target_repo=None):
    """Run agent.run() with all GitHub/metrics side effects stubbed out."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "brujoand/gitops-homelab")
    monkeypatch.setenv("ISSUE_NUMBER", "42")
    monkeypatch.delenv("TARGET_KIND", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    if target_repo:
        monkeypatch.setenv("AGENT_TARGET_REPO", target_repo)
    else:
        monkeypatch.delenv("AGENT_TARGET_REPO", raising=False)

    provider = FakeProvider(session)
    monkeypatch.setattr(agent, "create_provider", lambda name: provider)
    monkeypatch.setattr(agent, "post_announcement", lambda *a, **k: None)
    monkeypatch.setattr(
        agent, "post_comment", lambda view, issue, repo, body: comments.append(body)
    )
    monkeypatch.setattr(agent, "gh", lambda *a, **k: "")
    monkeypatch.setattr(agent, "open_pr_for_issue", lambda repo, issue: None)
    monkeypatch.setattr(agent.UsageTracker, "push", lambda self: None)
    return anyio.run(agent.run), provider


def test_run_done_on_first_turn(monkeypatch):
    session = FakeSession(
        [
            TurnResult(
                text="<<<DONE>>>\nFixed it in #99.\n<<<END_DONE>>>",
                usage=TurnUsage(num_turns=1),
                session_id="sess-1",
            )
        ]
    )
    comments = []
    code, provider = run_loop(monkeypatch, session, comments)

    assert code == 0
    # The provider got a neutral SessionConfig with the AGENT_MODEL default.
    assert provider.config == SessionConfig(
        model="claude-opus-4-8",
        cwd=provider.config.cwd,
        session_id=agent.session_id_for("brujoand/gitops-homelab", "42"),
        resume=False,
    )
    # One seed prompt, one closing comment carrying the model's summary.
    assert len(session.prompts) == 1
    assert "#42" in session.prompts[0]
    assert len(comments) == 1
    assert "Fixed it in #99." in comments[0]
    assert "Session ended" in comments[0]


def test_run_done_comment_carries_run_record(monkeypatch):
    monkeypatch.setenv("TRIGGER_SOURCE", "autopilot-schedule")
    session = FakeSession(
        [TurnResult(text="<<<DONE>>>\nDone.\n<<<END_DONE>>>", usage=TurnUsage(num_turns=1))]
    )
    comments = []
    code, _ = run_loop(monkeypatch, session, comments)

    assert code == 0
    # The closing comment is auditable: outcome + trigger provenance.
    assert "Run completed" in comments[0]
    assert "autopilot-schedule" in comments[0]


def test_run_nudges_when_no_marker(monkeypatch):
    session = FakeSession(
        [
            TurnResult(text="just rambling, no markers", usage=None),
            TurnResult(text="<<<DONE>>>", usage=TurnUsage(num_turns=1)),
        ]
    )
    comments = []
    code, _ = run_loop(monkeypatch, session, comments)

    assert code == 0
    assert len(session.prompts) == 2
    assert "did not emit" in session.prompts[1]
    # Bare <<<DONE>>> still closes the session with the footer-only comment.
    assert len(comments) == 1
    assert "Session ended" in comments[0]


def test_agent_target_repo_overrides_github_repository(monkeypatch):
    # The hub runs one workflow against many repos; AGENT_TARGET_REPO is what the
    # agent must operate on, not the workflow's own GITHUB_REPOSITORY.
    session = FakeSession([TurnResult(text="<<<DONE>>>", usage=TurnUsage(num_turns=1))])
    code, provider = run_loop(monkeypatch, session, [], target_repo="brujoand/tracktor")
    assert code == 0
    # Session id (and thus everything downstream) keys on the TARGET repo.
    assert provider.config.session_id == agent.session_id_for("brujoand/tracktor", "42")
    assert "brujoand/tracktor" in session.prompts[0]


def test_aborts_after_repeated_errored_turns(monkeypatch):
    # A misconfig that makes every turn error must fail fast, not nudge-and-retry
    # until the runtime budget is spent.
    session = FakeSession(
        [TurnResult(text="boom", usage=TurnUsage(num_turns=1), is_error=True) for _ in range(6)]
    )
    comments = []
    code, _ = run_loop(monkeypatch, session, comments)

    assert code == 1
    assert len(session.prompts) == agent._MAX_CONSECUTIVE_ERRORS  # bailed, didn't drain
    assert any("repeated errors" in c for c in comments)
