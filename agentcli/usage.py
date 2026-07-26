"""Report what actually drives Claude Code token spend on this host.

The OTel export in `~/.claude/settings.json` already ships counters to Prometheus,
but counters answer "how much" and not "why". The number that predicts cost is
**resident context per turn**: every turn re-sends the whole conversation, so a
session's cost is the sum of its context size over its turns -- quadratic in turn
count, not linear. A session parked at 600k costs 7x per turn what the same work
costs at 90k, and no counter shows that.

So this reads the local transcripts (`~/.claude/projects/*/*.jsonl`, the same
files `/usage` and ccusage read) and reports the distribution: context per turn,
turns per session, and how much of the re-read total comes from turns above a
threshold. That is the measurement that tells you whether to clear sessions, cap
the context window, or delegate to subagents -- rather than guessing.

Two deliberate limits, both stated in the output rather than papered over:

- **Costs are Anthropic list prices, not a bill.** On a subscription the flat rate
  absorbs them. The figure is an API-equivalent yardstick for comparing sessions
  and repos against each other; it is not what anyone charged. PRICES is a dated
  snapshot -- re-check `claude.com/pricing` before trusting a total.
- **Only what the transcripts hold.** Subagent turns and headless runs (the issue
  agent, which reports through its own UsageTracker) are not in these files, so
  the totals here are a floor.
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# Input $/MTok per model tier -- output is derived, so a price change is one edit.
# Snapshot: 2026-07-25, Anthropic first-party list prices. Sonnet's introductory
# $2/$10 (through 2026-08-31) is NOT applied; the standard rate keeps the yardstick
# stable across the reversion.
PRICES: dict[str, tuple[float, float]] = {
    "fable": (10.0, 50.0),
    "mythos": (10.0, 50.0),
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}
PRICES_AS_OF = "2026-07-25"

# Fast mode (`/fast`) runs the same model at a premium, and the transcript records
# it per turn as `usage.speed == "fast"`. Without this the report prices those
# turns at the standard rate and under-states them 2x -- silently, on exactly the
# turns you would most want to see. Opus-tier only: a tier absent here falls back
# to its standard price, because a missing entry means "fast mode does not exist
# on this tier", not "this traffic is free".
FAST_PRICES: dict[str, tuple[float, float]] = {
    "opus": (10.0, 50.0),
}

# Cache multipliers on the tier's input price. A 5-minute write costs 1.25x and a
# read 0.1x; the 1h-TTL write (2x) is not distinguishable in the transcript, so
# writes are priced at the 5-minute rate and a 1h-heavy workload reads low.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

# Context buckets in thousands of tokens, as (lower, upper) with upper exclusive.
# The top bucket is open-ended: the 1M-context models put turns above 1000k.
CONTEXT_BUCKETS: tuple[tuple[int, float], ...] = (
    (0, 100),
    (100, 200),
    (200, 400),
    (400, 700),
    (700, math.inf),
)

# Where the "high context" headline splits. 200k is the standard context window,
# so it is also the line between "this would fit without the 1M variant" and not.
HIGH_CONTEXT_THRESHOLD = 200_000


@dataclass(frozen=True)
class Turn:
    """One billed assistant turn: what it cost and how much context it re-read."""

    day: str
    project: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_write: int
    cache_read: int
    # `usage.speed` as the transcript reported it: "standard" or "fast". Defaulted
    # so a turn constructed without it prices as standard, which is what every
    # pre-fast-mode transcript means.
    speed: str = "standard"

    @property
    def context(self) -> int:
        """Resident context re-sent on this turn -- fresh input plus both cache halves."""
        return self.input_tokens + self.cache_write + self.cache_read

    @property
    def cost(self) -> float:
        """API-list-equivalent dollars. Zero for a tier we have no price for."""
        key = tier(self.model)
        price = FAST_PRICES.get(key) if self.speed == "fast" else None
        if price is None:
            price = PRICES.get(key)
        if price is None:
            return 0.0
        per_input, per_output = price
        return (
            self.input_tokens * per_input
            + self.output_tokens * per_output
            + self.cache_write * per_input * CACHE_WRITE_MULTIPLIER
            + self.cache_read * per_input * CACHE_READ_MULTIPLIER
        ) / 1e6


def tier(model: str) -> str:
    """Map a model id onto a PRICES key. Unknown ids stay unknown, never guessed.

    Silently defaulting an unrecognized model to a tier would put a wrong number
    in the total with no way to see it; the report counts unpriced turns instead.
    """
    name = (model or "").lower()
    for key in PRICES:
        if key in name:
            return key
    return "unknown"


def projects_dir() -> Path:
    """Claude Code's transcript root. Honors CLAUDE_CONFIG_DIR as Claude Code does."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".claude"
    return root / "projects"


def project_label(cwd: str, fallback: str) -> str:
    """A readable name for where a session ran, from its cwd.

    The transcript directory name is a flattened path (`-home-claude-src-agent`),
    which is ambiguous once worktrees are involved -- their slugs contain dashes
    too. The per-line `cwd` is unambiguous, so prefer it and mark worktrees so
    per-repo totals do not read as if all the work happened in the checkout.
    """
    parts = Path(cwd).parts if cwd else ()
    for anchor, suffix in (("worktrees", " (worktree)"), ("src", "")):
        if anchor in parts:
            index = parts.index(anchor)
            if index + 1 < len(parts):
                return parts[index + 1] + suffix
    return Path(cwd).name if cwd else fallback


def load_turns(days: int = 30) -> tuple[list[Turn], list[list[Turn]]]:
    """Every billed turn in the window, plus the turns grouped per session file.

    Returns both because the two questions need different shapes: cost aggregates
    over all turns, while turns-per-session and peak context are per-transcript.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    root = projects_dir()
    if not root.is_dir():
        return [], []

    turns: list[Turn] = []
    sessions: list[list[Turn]] = []
    for path in sorted(root.glob("*/*.jsonl")):
        session = list(_read_session(path, cutoff))
        if session:
            turns.extend(session)
            sessions.append(session)
    return turns, sessions


def _read_session(path: Path, cutoff: str):
    """Billed turns from one transcript. Malformed lines are skipped, not fatal.

    The `"usage"` substring test is a cheap prefilter: only assistant turns carry
    usage, and it keeps us from parsing the (much larger) tool-result lines at all.
    """
    fallback = path.parent.name
    try:
        handle = path.open(errors="ignore")
    except OSError:
        return
    with handle:
        for line in handle:
            if '"usage"' not in line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            day = str(record.get("timestamp") or "")[:10]
            if day and day < cutoff:
                continue
            message = record.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict) or not usage:
                continue
            yield Turn(
                day=day or "unknown",
                project=project_label(str(record.get("cwd") or ""), fallback),
                model=str(message.get("model") or "unknown"),
                input_tokens=_count(usage, "input_tokens"),
                output_tokens=_count(usage, "output_tokens"),
                cache_write=_count(usage, "cache_creation_input_tokens"),
                cache_read=_count(usage, "cache_read_input_tokens"),
                speed=str(usage.get("speed") or "standard"),
            )


def _count(usage: dict, key: str) -> int:
    """Token counts are absent on some turns and null on others; both mean zero."""
    value = usage.get(key)
    return value if isinstance(value, int) and value > 0 else 0


def percentile(values: list[int], quantile: float) -> int:
    """Lower-interpolated percentile (numpy's `interpolation="lower"`).

    Values must already be sorted; an empty list is zero rather than an error.
    """
    if not values:
        return 0
    return values[min(len(values) - 1, int(quantile * (len(values) - 1)))]


def summarize(turns: list[Turn], sessions: list[list[Turn]]) -> dict:
    """Everything the report shows, as plain data -- so --json and the text agree."""
    contexts = sorted(turn.context for turn in turns)
    reread = sum(contexts)
    high = [size for size in contexts if size > HIGH_CONTEXT_THRESHOLD]

    buckets = []
    for lower, upper in CONTEXT_BUCKETS:
        sizes = [s for s in contexts if lower * 1000 <= s < upper * 1000]
        buckets.append(
            {
                "from_k": lower,
                "to_k": None if upper == math.inf else int(upper),
                "turns": len(sizes),
                "tokens": sum(sizes),
                "share": _share(sum(sizes), reread),
            }
        )

    by_day: Counter[str] = Counter()
    by_project: Counter[str] = Counter()
    project_tokens: Counter[str] = Counter()
    by_tier: Counter[str] = Counter()
    tier_turns: Counter[str] = Counter()
    for turn in turns:
        by_day[turn.day] += turn.cost
        by_project[turn.project] += turn.cost
        project_tokens[turn.project] += turn.context + turn.output_tokens
        by_tier[tier(turn.model)] += turn.context + turn.output_tokens
        tier_turns[tier(turn.model)] += 1

    turn_counts = sorted((len(s) for s in sessions), reverse=True)
    return {
        "turns": len(turns),
        "sessions": len(sessions),
        "active_days": len(by_day),
        "unpriced_turns": tier_turns.get("unknown", 0),
        "tokens": {
            "input": sum(t.input_tokens for t in turns),
            "output": sum(t.output_tokens for t in turns),
            "cache_write": sum(t.cache_write for t in turns),
            "cache_read": sum(t.cache_read for t in turns),
        },
        "cost": {
            "total": sum(by_day.values()),
            "input": sum(t.input_tokens * _input_price(t) for t in turns) / 1e6,
            "output": sum(t.output_tokens * _output_price(t) for t in turns) / 1e6,
            "cache_write": sum(
                t.cache_write * _input_price(t) * CACHE_WRITE_MULTIPLIER for t in turns
            )
            / 1e6,
            "cache_read": sum(t.cache_read * _input_price(t) * CACHE_READ_MULTIPLIER for t in turns)
            / 1e6,
        },
        "context": {
            "median": percentile(contexts, 0.5),
            "p90": percentile(contexts, 0.9),
            "max": contexts[-1] if contexts else 0,
            "mean": int(reread / len(contexts)) if contexts else 0,
            "buckets": buckets,
            "high_turns": len(high),
            "high_share": _share(sum(high), reread),
        },
        # Hit rate over the two cache halves only: fresh input is a cache concern
        # of a different kind (nothing to hit yet), and including it would make a
        # long, healthy session look like it was missing.
        "cache_hit_rate": _share(
            sum(t.cache_read for t in turns),
            sum(t.cache_read + t.cache_write for t in turns),
        ),
        "session_turns": {
            "median": percentile(sorted(turn_counts), 0.5),
            "p90": percentile(sorted(turn_counts), 0.9),
            "max": turn_counts[0] if turn_counts else 0,
        },
        "by_day": dict(sorted(by_day.items())),
        "by_project": [
            {"project": name, "cost": cost, "tokens": project_tokens[name]}
            for name, cost in by_project.most_common()
        ],
        "by_tier": [
            {"tier": name, "tokens": tokens, "turns": tier_turns[name]}
            for name, tokens in by_tier.most_common()
        ],
    }


def _input_price(turn: Turn) -> float:
    return PRICES.get(tier(turn.model), (0.0, 0.0))[0]


def _output_price(turn: Turn) -> float:
    return PRICES.get(tier(turn.model), (0.0, 0.0))[1]


def _share(part: int | float, whole: int | float) -> float:
    """Percent, with an empty whole reported as zero rather than dividing by it."""
    return 100.0 * part / whole if whole else 0.0


def render(report: dict, days: int, day_limit: int = 14) -> list[str]:
    """The text report, as lines. Leads with the lever, not with the total."""
    if not report["turns"]:
        return [f"no billed turns in the last {days}d under {projects_dir()}"]

    tokens, cost, context = report["tokens"], report["cost"], report["context"]
    lines = [
        f"last {days}d: {report['turns']} turns across {report['sessions']} sessions"
        f" on {report['active_days']} active days",
        "",
        f"context per turn: median {context['median'] // 1000}k"
        f"  mean {context['mean'] // 1000}k"
        f"  p90 {context['p90'] // 1000}k"
        f"  max {context['max'] // 1000}k",
        f"turns per session: median {report['session_turns']['median']}"
        f"  p90 {report['session_turns']['p90']}"
        f"  max {report['session_turns']['max']}",
        f"cache hit rate: {report['cache_hit_rate']:.1f}%",
        "",
        f"THE LEVER: {report['context']['high_turns']} turns above"
        f" {HIGH_CONTEXT_THRESHOLD // 1000}k context re-read"
        f" {context['high_share']:.0f}% of all re-read tokens.",
        "",
        "context distribution (share of re-read tokens):",
    ]
    for bucket in context["buckets"]:
        span = (
            f"{bucket['from_k']}-{bucket['to_k']}k" if bucket["to_k"] else f">{bucket['from_k']}k"
        )
        lines.append(
            f"  {span:>12}  {bucket['turns']:6d} turns"
            f"  {bucket['tokens'] / 1e6:8.1f}M tok  {bucket['share']:5.1f}%"
        )

    lines += [
        "",
        "spend composition (API list-price equivalent):",
        f"  cache read   {tokens['cache_read'] / 1e6:9.1f}M  ${cost['cache_read']:9.2f}"
        f"  {_share(cost['cache_read'], cost['total']):5.1f}%",
        f"  cache write  {tokens['cache_write'] / 1e6:9.1f}M  ${cost['cache_write']:9.2f}"
        f"  {_share(cost['cache_write'], cost['total']):5.1f}%",
        f"  output       {tokens['output'] / 1e6:9.1f}M  ${cost['output']:9.2f}"
        f"  {_share(cost['output'], cost['total']):5.1f}%",
        f"  fresh input  {tokens['input'] / 1e6:9.1f}M  ${cost['input']:9.2f}"
        f"  {_share(cost['input'], cost['total']):5.1f}%",
        f"  TOTAL                    ${cost['total']:9.2f}"
        f"  (${cost['total'] / max(1, report['active_days']):.2f}/active day)",
        "",
        "by model tier:",
    ]
    for entry in report["by_tier"]:
        lines.append(
            f"  {entry['tier']:<10} {entry['tokens'] / 1e6:9.1f}M tok  {entry['turns']:6d} turns"
        )

    lines += ["", "by repo:"]
    for entry in report["by_project"]:
        lines.append(
            f"  ${entry['cost']:9.2f}  {entry['tokens'] / 1e6:8.1f}M tok  {entry['project']}"
        )

    recent = list(report["by_day"].items())[-day_limit:]
    lines += ["", f"per day (last {len(recent)} active):"]
    for day, amount in recent:
        lines.append(f"  {day}  ${amount:8.2f}")

    lines += [
        "",
        f"List prices as of {PRICES_AS_OF}; a subscription absorbs them, so this is a"
        " yardstick for comparing sessions, not a bill.",
        "Subagent turns and headless runs are not in these transcripts -- treat the"
        " total as a floor.",
    ]
    if report["unpriced_turns"]:
        lines.append(
            f"{report['unpriced_turns']} turn(s) ran on a model with no price entry"
            " and contribute tokens but no cost."
        )
    return lines


def run(days: int = 30, as_json: bool = False) -> int:
    turns, sessions = load_turns(days)
    report = summarize(turns, sessions)
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for line in render(report, days):
            print(line)
    return 0
