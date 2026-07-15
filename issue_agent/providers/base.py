"""Provider-neutral agent-session protocol and types.

This module is the seam between the issue-agent wrapper (agent.py) and
whatever backend runs the actual agent loop. The wrapper only ever needs to:
probe whether a persisted session exists (resume), open ONE session, send a
prompt per turn, and read back the turn's text plus usage accounting. Keep the
surface exactly that small — anything a specific backend needs beyond it
(session stores, tool allowlists, permission modes) belongs in that backend's
adapter module, not here.

No provider SDK imports in this module — it must be importable in any venv.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SessionConfig:
    """Provider-neutral knobs for one agent session."""

    # AGENT_MODEL passes through untouched; each adapter interprets it in its
    # own namespace (e.g. "claude-opus-4-8" vs an Ollama tag).
    model: str
    # The repo checkout the agent operates on.
    cwd: str
    # Deterministic id derived from repo + issue/PR number.
    session_id: str
    # Continue a persisted session instead of starting fresh.
    resume: bool = False
    # The session's role: "issue" (triage/fix) or "pr" (address review feedback).
    # Provider-neutral on purpose — the wrapper knows the role (TARGET_KIND) but
    # not what it implies; each adapter maps it to its own policy (e.g. the Claude
    # adapter narrows the tool allowlist for "pr"). Unknown values are the
    # adapter's to interpret; the default keeps single-mode callers unchanged.
    kind: str = "issue"


@dataclass(frozen=True)
class TurnUsage:
    """Token/cost/turn accounting for one completed turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0


@dataclass(frozen=True)
class TurnResult:
    """Everything the wrapper needs from one turn."""

    # All assistant text blocks of the turn, "\n"-joined.
    text: str
    # None when the provider reported no usage/result for the turn.
    usage: TurnUsage | None
    session_id: str | None = None
    is_error: bool = False


@runtime_checkable
class AgentSession(Protocol):
    """One live multi-turn conversation. Async context manager."""

    async def __aenter__(self) -> AgentSession: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...

    async def run_turn(self, prompt: str) -> TurnResult:
        """Send one prompt, drive the provider's internal agent loop to
        completion for this turn, and return the collected result."""
        ...


@runtime_checkable
class AgentProvider(Protocol):
    async def session_exists(self, session_id: str, cwd: str) -> bool:
        """True if a persisted transcript exists for this session, i.e. the
        wrapper should resume rather than start fresh. Providers without
        persistence return False — every run starts fresh and the wrapper's
        resume-prompt path simply never fires."""
        ...

    def open_session(self, config: SessionConfig) -> AgentSession: ...
