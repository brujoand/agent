from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from agentcli import usage


def _turn(day: str, usage_fields: dict, model: str = "claude-opus-4-8", cwd: str = "") -> str:
    """One transcript line in the shape Claude Code writes."""
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": f"{day}T12:00:00.000Z",
            "cwd": cwd,
            "message": {"role": "assistant", "model": model, "usage": usage_fields},
        }
    )


@pytest.fixture
def transcripts(monkeypatch, tmp_path):
    """An empty transcript root wired into the module, plus a writer for sessions."""
    root = tmp_path / "projects"
    root.mkdir(parents=True)
    monkeypatch.setattr(usage, "projects_dir", lambda: root)

    def write(project: str, session: str, lines: list[str]) -> None:
        directory = root / project
        directory.mkdir(exist_ok=True)
        (directory / f"{session}.jsonl").write_text("\n".join(lines) + "\n")

    return write


def test_tier_never_guesses_an_unknown_model():
    assert usage.tier("claude-opus-4-8") == "opus"
    assert usage.tier("claude-haiku-4-5-20251001") == "haiku"
    assert usage.tier("claude-fable-5") == "fable"
    assert usage.tier("<synthetic>") == "unknown"
    assert usage.tier("") == "unknown"


def test_cost_applies_the_cache_multipliers_to_the_tier_price():
    turn = usage.Turn(
        day="2026-07-25",
        project="agent",
        model="claude-opus-4-8",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_write=1_000_000,
        cache_read=1_000_000,
    )
    # opus is $5 in / $25 out; write 1.25x input, read 0.1x input.
    assert turn.cost == pytest.approx(5.0 + 25.0 + 6.25 + 0.5)
    assert turn.context == 3_000_000


def test_cost_is_zero_for_a_model_with_no_price_entry():
    turn = usage.Turn("2026-07-25", "agent", "<synthetic>", 1_000_000, 1_000_000, 0, 0)
    assert turn.cost == 0.0
    # The tokens still count, so an unpriced model cannot hide traffic.
    assert turn.context == 1_000_000


def test_load_turns_skips_lines_outside_the_window(transcripts):
    today = date.today().isoformat()
    old = (date.today() - timedelta(days=45)).isoformat()
    transcripts(
        "-home-claude-src-agent",
        "s1",
        [
            _turn(today, {"input_tokens": 10, "cache_read_input_tokens": 100}),
            _turn(old, {"input_tokens": 99, "cache_read_input_tokens": 999}),
        ],
    )

    turns, sessions = usage.load_turns(days=30)

    assert [t.cache_read for t in turns] == [100]
    assert len(sessions) == 1


def test_load_turns_ignores_malformed_and_usageless_lines(transcripts):
    today = date.today().isoformat()
    transcripts(
        "-home-claude-src-agent",
        "s1",
        [
            "{ this is not json but mentions usage",
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
            json.dumps({"timestamp": f"{today}T00:00:00Z", "message": {"usage": {}}}),
            _turn(today, {"input_tokens": 5, "output_tokens": 7}),
        ],
    )

    turns, _ = usage.load_turns(days=30)

    assert len(turns) == 1
    assert turns[0].output_tokens == 7


def test_null_token_counts_read_as_zero(transcripts):
    today = date.today().isoformat()
    transcripts(
        "-home-claude-src-agent",
        "s1",
        [_turn(today, {"input_tokens": None, "cache_read_input_tokens": 42})],
    )

    turns, _ = usage.load_turns(days=30)

    assert turns[0].input_tokens == 0
    assert turns[0].cache_read == 42


def test_project_label_distinguishes_worktrees_from_checkouts():
    assert usage.project_label("/home/claude/src/agent", "fallback") == "agent"
    assert (
        usage.project_label("/home/claude/worktrees/agent/session-usage-command", "fallback")
        == "agent (worktree)"
    )
    assert usage.project_label("", "-home-claude-src-agent") == "-home-claude-src-agent"


def test_load_turns_returns_empty_when_there_is_no_transcript_root(monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "projects_dir", lambda: tmp_path / "absent")
    assert usage.load_turns(days=30) == ([], [])


def test_percentile_interpolates_low_and_tolerates_empty():
    assert usage.percentile([], 0.5) == 0
    assert usage.percentile([1, 2, 3, 4, 5], 0.5) == 3
    # Lower interpolation: index int(0.9 * 4) == 3, so p90 of five values is the 4th.
    assert usage.percentile([1, 2, 3, 4, 5], 0.9) == 4
    assert usage.percentile([1, 2, 3, 4, 5], 1.0) == 5
    assert usage.percentile([7], 0.9) == 7


def test_summarize_reports_the_high_context_share_and_hit_rate(transcripts):
    today = date.today().isoformat()
    # One cheap turn at 50k context and one expensive one at 500k: the big turn
    # holds 10/11 of the re-read total, which is the share the report leads with.
    transcripts(
        "-home-claude-src-agent",
        "s1",
        [
            _turn(today, {"cache_read_input_tokens": 50_000}, cwd="/home/claude/src/agent"),
            _turn(today, {"cache_read_input_tokens": 500_000}, cwd="/home/claude/src/agent"),
        ],
    )

    turns, sessions = usage.load_turns(days=30)
    report = usage.summarize(turns, sessions)

    assert report["turns"] == 2
    assert report["sessions"] == 1
    assert report["context"]["high_turns"] == 1
    assert report["context"]["high_share"] == pytest.approx(100 * 500 / 550)
    # No cache writes at all, so every cached token was a hit.
    assert report["cache_hit_rate"] == pytest.approx(100.0)
    assert report["by_project"][0]["project"] == "agent"


def test_summarize_buckets_cover_every_turn_including_above_a_megatoken(transcripts):
    today = date.today().isoformat()
    sizes = [10_000, 150_000, 300_000, 500_000, 900_000, 1_050_000]
    transcripts(
        "-home-claude-src-agent",
        "s1",
        [_turn(today, {"cache_read_input_tokens": size}) for size in sizes],
    )

    turns, sessions = usage.load_turns(days=30)
    report = usage.summarize(turns, sessions)

    assert sum(b["turns"] for b in report["context"]["buckets"]) == len(sizes)
    # The open-ended top bucket has to catch both the 900k and the 1.05M turn.
    assert report["context"]["buckets"][-1]["turns"] == 2
    assert sum(b["share"] for b in report["context"]["buckets"]) == pytest.approx(100.0)


def test_summarize_counts_unpriced_turns_separately(transcripts):
    today = date.today().isoformat()
    transcripts(
        "-home-claude-src-agent",
        "s1",
        [
            _turn(today, {"output_tokens": 100}, model="claude-opus-4-8"),
            _turn(today, {"output_tokens": 100}, model="<synthetic>"),
        ],
    )

    turns, sessions = usage.load_turns(days=30)
    report = usage.summarize(turns, sessions)

    assert report["unpriced_turns"] == 1
    assert report["cost"]["total"] == pytest.approx(100 * 25.0 / 1e6)


def test_session_turn_counts_are_per_transcript(transcripts):
    today = date.today().isoformat()
    transcripts("-p", "long", [_turn(today, {"output_tokens": 1})] * 5)
    transcripts("-p", "short", [_turn(today, {"output_tokens": 1})])

    turns, sessions = usage.load_turns(days=30)
    report = usage.summarize(turns, sessions)

    assert report["turns"] == 6
    assert report["session_turns"]["max"] == 5


def test_render_reports_an_empty_window_without_dividing_by_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "projects_dir", lambda: tmp_path / "absent")
    report = usage.summarize([], [])

    lines = usage.render(report, days=30)

    assert len(lines) == 1
    assert "no billed turns" in lines[0]


def test_render_leads_with_the_lever(transcripts):
    today = date.today().isoformat()
    transcripts(
        "-home-claude-src-agent",
        "s1",
        [_turn(today, {"cache_read_input_tokens": 400_000, "output_tokens": 2_000})],
    )

    turns, sessions = usage.load_turns(days=30)
    text = "\n".join(usage.render(usage.summarize(turns, sessions), days=30))

    assert "THE LEVER" in text
    assert "context per turn" in text
    # The caveats are load-bearing: the number is not a bill and not a ceiling.
    assert "not a bill" in text
    assert "floor" in text


def test_run_json_emits_parseable_report(transcripts, capsys):
    today = date.today().isoformat()
    transcripts("-home-claude-src-agent", "s1", [_turn(today, {"output_tokens": 10})])

    assert usage.run(days=30, as_json=True) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["turns"] == 1
