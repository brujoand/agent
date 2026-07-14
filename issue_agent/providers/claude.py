"""Claude Agent SDK adapter.

Everything Claude-specific lives here: the claude-agent-sdk client, the
MinIO-backed session store (how THIS harness persists/resumes transcripts),
and the Claude Code harness options (tool allowlist, permission mode). The
wrapper in agent.py only sees the provider-neutral protocol from base.py.

Imports resolve script-relative (sys.path[0] == /opt/issue-agent at runtime;
tests/conftest.py replicates that), so s3_session_store is a flat module.
"""

from __future__ import annotations

import os
import sys
from types import TracebackType

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    project_key_for_directory,
)
from s3_session_store import S3SessionStore

from providers.base import SessionConfig, TurnResult, TurnUsage

# Cost ceiling per query; wall-clock MAX_RUNTIME_SECONDS still bounds the
# whole session.
MAX_TURNS = 50

ALLOWED_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Edit",
    "Write",
    "Task",
    "Bash(git:*)",
    "Bash(gh:*)",
    "Bash(pre-commit:*)",
    "Bash(mise:*)",
    "Bash(kubectl:*)",
    # Query the in-cluster observability HTTP APIs directly. `lab` would
    # need pods/exec + port-forward (privilege the read-only `view` SA
    # deliberately lacks); curling the Service endpoints stays read-only.
    "Bash(curl:*)",
]


def _env_required(name: str) -> str:
    # Local copy of agent.env()'s required path: claude.py cannot import from
    # agent.py (agent -> providers -> agent cycle). Same FATAL message and
    # exit code so operators see identical failures.
    val = os.environ.get(name)
    if not val:
        print(f"FATAL: missing required env {name}", file=sys.stderr)
        sys.exit(2)
    return val


def make_store() -> S3SessionStore:
    import boto3  # lazy: only the live adapter needs it, tests inject a store

    client = boto3.client(
        "s3",
        endpoint_url=_env_required("MINIO_ENDPOINT_URL"),
        aws_access_key_id=_env_required("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=_env_required("AWS_SECRET_ACCESS_KEY"),
        region_name="us-east-1",  # ignored by MinIO but boto3 wants one
    )
    return S3SessionStore(
        bucket=_env_required("MINIO_BUCKET"),
        prefix="transcripts",
        client=client,
    )


class ClaudeProvider:
    def __init__(self, store: S3SessionStore | None = None) -> None:
        self._store = store if store is not None else make_store()

    async def session_exists(self, session_id: str, cwd: str) -> bool:
        # The SDK keys transcripts by a cwd-derived project_key (not the repo
        # slug), so derive it with the SDK's own helper and load the exact key.
        project_key = project_key_for_directory(cwd)
        existing = await self._store.load({"project_key": project_key, "session_id": session_id})
        return bool(existing)

    def open_session(self, config: SessionConfig) -> ClaudeSession:
        opts = ClaudeAgentOptions(
            model=config.model,
            max_turns=MAX_TURNS,
            permission_mode="acceptEdits",
            setting_sources=["project"],  # load CLAUDE.md + .claude/agents/
            allowed_tools=ALLOWED_TOOLS,
            session_store=self._store,
            # Operate on the checked-out repo (Actions sets GITHUB_WORKSPACE),
            # not the wrapper's own dir.
            cwd=config.cwd,
            **(
                {"resume": config.session_id}
                if config.resume
                else {"session_id": config.session_id}
            ),
        )
        return ClaudeSession(opts)


class ClaudeSession:
    def __init__(self, opts: ClaudeAgentOptions) -> None:
        self._client = ClaudeSDKClient(options=opts)

    async def __aenter__(self) -> ClaudeSession:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        return await self._client.__aexit__(exc_type, exc, tb)

    async def run_turn(self, prompt: str) -> TurnResult:
        await self._client.query(prompt)
        messages = [m async for m in self._client.receive_response()]
        texts = [
            block.text
            for msg in messages
            if isinstance(msg, AssistantMessage)
            for block in msg.content
            if isinstance(block, TextBlock)
        ]
        result = next((m for m in messages if isinstance(m, ResultMessage)), None)
        usage: TurnUsage | None = None
        session_id: str | None = None
        is_error = False
        if result is not None:
            u = result.usage or {}
            usage = TurnUsage(
                input_tokens=int(u.get("input_tokens") or 0),
                output_tokens=int(u.get("output_tokens") or 0),
                cache_creation_input_tokens=int(u.get("cache_creation_input_tokens") or 0),
                cache_read_input_tokens=int(u.get("cache_read_input_tokens") or 0),
                cost_usd=float(result.total_cost_usd or 0.0),
                num_turns=int(result.num_turns or 0),
            )
            session_id = result.session_id
            is_error = bool(result.is_error)
        return TurnResult(
            text="\n".join(texts),
            usage=usage,
            session_id=session_id,
            is_error=is_error,
        )
