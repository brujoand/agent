"""Claude Agent SDK adapter.

Everything Claude-specific lives here: the claude-agent-sdk client, an optional
MinIO-backed session store (how THIS harness persists/resumes transcripts; when
MinIO is unconfigured the session runs stateless), and the Claude Code harness
options (tool allowlist, permission mode). The wrapper in agent.py only sees the
provider-neutral protocol from base.py.

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

# Tools every session gets regardless of role: read/search, edit, delegate to a
# subagent, and the git/gh/pre-commit/mise plumbing both the issue and PR agents
# need to investigate a repo and open or update a PR.
_COMMON_TOOLS = [
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
]

# Live-cluster read access. Only the issue/triage agent inspects cluster state
# (kubectl) and curls the in-cluster observability HTTP APIs directly — `lab`
# would need pods/exec + port-forward (privilege the read-only `view` SA
# deliberately lacks), so curling the Service endpoints stays read-only. The PR
# agent works a checked-out diff on a feature branch and has no business reaching
# the live cluster, so it does not get these (least privilege per role).
_CLUSTER_READ_TOOLS = [
    "Bash(kubectl:*)",
    "Bash(curl:*)",
]

# SessionConfig.kind -> allowed tools. "issue" keeps today's full set unchanged;
# "pr" drops live-cluster reads.
TOOL_POLICY = {
    "issue": _COMMON_TOOLS + _CLUSTER_READ_TOOLS,
    "pr": _COMMON_TOOLS,
}


def allowed_tools_for(kind: str) -> list[str]:
    """Tool allowlist for a session role. An unknown kind falls back to the
    broadest ("issue") policy: the wrapper only ever passes "issue"/"pr", and a
    future mode should fail open to today's behaviour, not silently lose tools."""
    return TOOL_POLICY.get(kind, TOOL_POLICY["issue"])


def _env_required(name: str) -> str:
    # Local copy of agent.env()'s required path: claude.py cannot import from
    # agent.py (agent -> providers -> agent cycle). Same FATAL message and
    # exit code so operators see identical failures.
    val = os.environ.get(name)
    if not val:
        print(f"FATAL: missing required env {name}", file=sys.stderr)
        sys.exit(2)
    return val


def make_store() -> S3SessionStore | None:
    """The MinIO/S3 transcript store, or None when MinIO is not configured.

    Persistence (and cross-timeout resume) is opt-in: without MINIO_ENDPOINT_URL
    the runtime runs stateless — a fresh session each run, no resume. When the
    endpoint IS set, the bucket and AWS creds become required."""
    endpoint = os.environ.get("MINIO_ENDPOINT_URL")
    if not endpoint:
        return None

    import boto3  # lazy: only the live adapter needs it, tests inject a store

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
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
        # None from make_store() means "stateless" (no MinIO configured), which is
        # a valid mode — not "use the default store". Tests inject a fake store.
        self._store = store if store is not None else make_store()

    async def session_exists(self, session_id: str, cwd: str) -> bool:
        # Stateless (no store) never resumes: there is no persisted transcript.
        if self._store is None:
            return False
        # The SDK keys transcripts by a cwd-derived project_key (not the repo
        # slug), so derive it with the SDK's own helper and load the exact key.
        project_key = project_key_for_directory(cwd)
        existing = await self._store.load({"project_key": project_key, "session_id": session_id})
        return bool(existing)

    def open_session(self, config: SessionConfig) -> ClaudeSession:
        # Persistence is optional: pass session_store only when configured, and
        # only resume when there is a store to resume from.
        store_kwargs = {"session_store": self._store} if self._store is not None else {}
        resume = config.resume and self._store is not None
        opts = ClaudeAgentOptions(
            model=config.model,
            max_turns=MAX_TURNS,
            permission_mode="acceptEdits",
            setting_sources=["project"],  # load CLAUDE.md + .claude/agents/
            allowed_tools=allowed_tools_for(config.kind),
            # Operate on the checked-out repo (Actions sets GITHUB_WORKSPACE),
            # not the wrapper's own dir.
            cwd=config.cwd,
            **store_kwargs,
            **({"resume": config.session_id} if resume else {"session_id": config.session_id}),
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
