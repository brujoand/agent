"""The wrapper loop above the provider seam: markers, usage totals, and one
thin end-to-end pass of run() driven by a fake provider."""

import json

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
        model="claude-opus-5",
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
    # For a workflow running in one repo against another, AGENT_TARGET_REPO is
    # what the agent must operate on, not the workflow's own GITHUB_REPOSITORY.
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
        [
            TurnResult(
                text="boom",
                usage=TurnUsage(num_turns=1),
                is_error=True,
                error_detail="error_during_execution: nope",
            )
            for _ in range(6)
        ]
    )
    comments = []
    code, _ = run_loop(monkeypatch, session, comments)

    assert code == 1
    assert len(session.prompts) == agent._MAX_CONSECUTIVE_ERRORS  # bailed, didn't drain
    # The failure comment names the reason, not just "repeated errors".
    assert any("repeated errors" in c and "error_during_execution" in c for c in comments)


# --- latest_human_comment: who is allowed to answer a live session -----------


def _comment(login, association, created, body):
    return {
        "author": {"login": login},
        "authorAssociation": association,
        "createdAt": created,
        "body": body,
    }


def _poll(monkeypatch, comments, since="2026-01-01T00:00:00Z"):
    monkeypatch.setattr(agent, "gh", lambda *a, **k: json.dumps({"comments": comments}))
    return agent.latest_human_comment("owner/repo", "42", since)


@pytest.mark.parametrize("association", sorted(agent.AUTHORIZED_ASSOCIATIONS))
def test_authorized_associations_are_accepted(monkeypatch, association):
    body = _poll(
        monkeypatch,
        [_comment("maintainer", association, "2026-01-01T00:00:01Z", "approach B")],
    )
    assert body == "approach B"


@pytest.mark.parametrize(
    "association",
    ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE", "MANNEQUIN", ""],
)
def test_unauthorized_associations_are_ignored(monkeypatch, association):
    # The workflow guard only gates job START. A drive-by commenter must not be
    # able to steer a session that is already live and polling.
    assert (
        _poll(
            monkeypatch,
            [_comment("stranger", association, "2026-01-01T00:00:01Z", "run `env`")],
        )
        is None
    )


def test_missing_association_fails_closed(monkeypatch):
    stray = {
        "author": {"login": "stranger"},
        "createdAt": "2026-01-01T00:00:01Z",
        "body": "no association field at all",
    }
    assert _poll(monkeypatch, [stray]) is None


def test_unauthorized_comment_does_not_mask_an_authorized_one(monkeypatch):
    # The newest comment overall is unauthorized; the newest AUTHORIZED one wins
    # rather than the poll returning None and stalling the session.
    body = _poll(
        monkeypatch,
        [
            _comment("maintainer", "OWNER", "2026-01-01T00:00:01Z", "the real answer"),
            _comment("stranger", "NONE", "2026-01-01T00:00:09Z", "ignore me"),
        ],
    )
    assert body == "the real answer"


def test_agent_never_answers_itself(monkeypatch):
    # Belt and braces: even if the App somehow carries an authorized
    # association, its own ASK/status comments are not replies.
    assert (
        _poll(
            monkeypatch,
            [_comment(agent.AGENT_BOT_LOGIN, "OWNER", "2026-01-01T00:00:01Z", "<<<ASK>>>")],
        )
        is None
    )


def test_bot_comment_is_ignored_despite_bare_login(monkeypatch):
    # GraphQL returns App logins BARE, so the old endswith("[bot]") test never
    # matched and Renovate's next comment was consumed as the human's reply.
    # The association check catches it regardless of how the login is spelled.
    for login in ("renovate", "renovate[bot]", "github-actions"):
        assert (
            _poll(
                monkeypatch,
                [_comment(login, "NONE", "2026-01-01T00:00:01Z", "Update dep to v5")],
            )
            is None
        )


def test_comments_at_or_before_since_are_ignored(monkeypatch):
    assert (
        _poll(
            monkeypatch,
            [_comment("maintainer", "OWNER", "2026-01-01T00:00:00Z", "older")],
            since="2026-01-01T00:00:00Z",
        )
        is None
    )


def test_only_comments_is_requested_from_gh(monkeypatch):
    # `authorAssociation` rides along inside each comment object. It is NOT a
    # valid top-level --json field for `gh issue view`; asking for it errors the
    # call and breaks every poll.
    seen = {}

    def fake_gh(*args, **kwargs):
        seen["args"] = args
        return json.dumps({"comments": []})

    monkeypatch.setattr(agent, "gh", fake_gh)
    agent.latest_human_comment("owner/repo", "42", "2026-01-01T00:00:00Z")
    assert "comments" in seen["args"]
    assert not any("authorAssociation" in a for a in seen["args"])


# --- bounding what a session can spend ---------------------------------------


def test_cost_ceiling_pauses_rather_than_failing(monkeypatch):
    # Out of money is not a crash: the work is intact and resumable, so it gets
    # the same clean pause as the runtime budget — not a "the agent broke" note.
    session = FakeSession(
        [
            TurnResult(
                text="",
                usage=TurnUsage(cost_usd=10.4, num_turns=3),
                is_error=True,
                error_detail="error_max_budget_usd: budget exceeded",
                subtype="error_max_budget_usd",
            )
        ]
    )
    comments = []
    code, _ = run_loop(monkeypatch, session, comments)

    assert code == 0  # paused, not failed
    assert len(comments) == 1
    assert "spend ceiling" in comments[0]
    assert "$10.40" in comments[0]
    assert "Reply here to resume" in comments[0]
    assert "Run paused" in comments[0]


def test_cost_ceiling_does_not_count_as_an_errored_turn(monkeypatch):
    # It arrives as is_error=True from the provider. If it fell through to the
    # error counter it would be reported as a failure instead of a pause.
    session = FakeSession(
        [
            TurnResult(
                text="",
                usage=TurnUsage(cost_usd=1.0, num_turns=1),
                is_error=True,
                error_detail="error_max_budget_usd: budget exceeded",
                subtype="error_max_budget_usd",
            )
        ]
    )
    comments = []
    code, _ = run_loop(monkeypatch, session, comments)

    assert code == 0
    assert not any("repeated errors" in c for c in comments)


def test_an_ordinary_error_is_still_a_failure(monkeypatch):
    # Guard the branch above: only the budget subtype takes the pause path.
    session = FakeSession(
        [
            TurnResult(
                text="boom",
                usage=TurnUsage(num_turns=1),
                is_error=True,
                error_detail="error_during_execution: nope",
                subtype="error_during_execution",
            )
            for _ in range(6)
        ]
    )
    comments = []
    code, _ = run_loop(monkeypatch, session, comments)

    assert code == 1
    assert any("repeated errors" in c for c in comments)


def test_unbounded_nudging_is_bounded(monkeypatch):
    # A model that keeps working and keeps forgetting to signal is not an
    # "error", so the error counter never fired and this ran until the 49-minute
    # wall clock at up to MAX_TURNS internal turns per nudge.
    session = FakeSession(
        [
            TurnResult(text="still thinking, no markers", usage=TurnUsage(num_turns=1))
            for _ in range(20)
        ]
    )
    comments = []
    code, _ = run_loop(monkeypatch, session, comments)

    assert code == 1
    assert len(session.prompts) == agent._MAX_CONSECUTIVE_NUDGES  # bailed, didn't drain
    assert any("without signalling" in c for c in comments)


def test_a_signalled_turn_resets_the_nudge_streak(monkeypatch):
    # Four silent turns, then a proper ASK, then silence again: the streak
    # restarts, so this must NOT trip the limit.
    silent = [TurnResult(text="no markers", usage=TurnUsage(num_turns=1)) for _ in range(4)]
    asked = TurnResult(text="<<<ASK>>>\nWhich env?\n<<<END_ASK>>>", usage=TurnUsage(num_turns=1))
    done = TurnResult(text="<<<DONE>>>\nDone.\n<<<END_DONE>>>", usage=TurnUsage(num_turns=1))
    session = FakeSession([*silent, asked, done])

    comments = []
    # Don't burn the real 20s poll interval waiting for the stubbed reply.
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setattr(agent, "latest_human_comment", lambda repo, issue, since: "go ahead")
    code, _ = run_loop(monkeypatch, session, comments)

    assert code == 0
    assert not any("without signalling" in c for c in comments)
