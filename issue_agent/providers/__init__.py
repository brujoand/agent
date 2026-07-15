"""Provider factory: AGENT_PROVIDER -> adapter.

Adapter imports are lazy so `import providers` (and therefore importing
agent.py) never pulls a provider SDK — only instantiating one does.
"""

from providers.base import (
    AgentProvider,
    AgentSession,
    SessionConfig,
    TurnResult,
    TurnUsage,
)

__all__ = [
    "AgentProvider",
    "AgentSession",
    "SessionConfig",
    "TurnResult",
    "TurnUsage",
    "create_provider",
]


def create_provider(name: str) -> AgentProvider:
    if name == "claude":
        from providers.claude import ClaudeProvider

        return ClaudeProvider()
    raise SystemExit(f"unknown AGENT_PROVIDER {name!r} (supported: claude)")
