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
from pathlib import Path
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

# House register, shared with the interactive setup.
#
# Interactively, Claude Code loads ~/.claude/output-styles/terse.md, symlinked
# there by `agent output-styles install`. This runtime never sees that: the image
# carries no user-level ~/.claude, and sessions open with
# setting_sources=["project"] on purpose. So the same file is read from the repo
# tree and appended to the claude_code system-prompt preset instead -- one source
# of truth, two delivery mechanisms.
OUTPUT_STYLE = "terse"

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

# SessionConfig.kind -> base allowed tools. Cluster-read tools are NOT in here;
# they are added separately and only when explicitly enabled (see below).
TOOL_POLICY = {
    "issue": _COMMON_TOOLS,
    "pr": _COMMON_TOOLS,
}


def allowed_tools_for(kind: str) -> list[str]:
    """Tool allowlist for a session role.

    Cluster-read tools (kubectl + curl to in-cluster APIs) are OPT-IN via
    ``AGENT_CLUSTER_TOOLS=1`` and only for the issue role. So the DEFAULT — any
    run that does not set the flag — gets NO cluster reach even in the allowlist,
    and only a deployment's own privileged workflow opts in. Keep it that way:
    this is defense in depth, meant to hold even where the runner already has no
    cluster credential to spend. An unknown kind falls back to the issue base set
    (never silently loses the git/gh plumbing)."""
    tools = list(TOOL_POLICY.get(kind, TOOL_POLICY["issue"]))
    if kind == "issue" and os.environ.get("AGENT_CLUSTER_TOOLS") == "1":
        tools += _CLUSTER_READ_TOOLS
    return tools


def style_dirs() -> list[Path]:
    """Where the output-styles tree can be, most specific first.

    `/opt/issue-agent/output-styles` in the image (the Dockerfile copies the tree
    in beside this wrapper), `<repo>/output-styles` in a checkout -- which is
    what the tests and a local run see. Two candidates rather than an env var:
    the file ships with the code, so it should be found the same way the code is.
    """
    here = Path(__file__).resolve()
    return [here.parents[1] / "output-styles", here.parents[2] / "output-styles"]


def _strip_frontmatter(text: str) -> str:
    """Drop a leading `---` block. Its keys are Claude Code picker metadata
    (name, description, keep-coding-instructions), not instructions, and the
    preset append takes prose."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    return text[end + 4 :] if end != -1 else text


def style_body(name: str = OUTPUT_STYLE) -> str:
    """The style's instructions, or "" if the tree is not there.

    Missing is a warning, not a fatal: a register tweak must never be the thing
    that stops the agent from answering an issue.
    """
    for directory in style_dirs():
        path = directory / f"{name}.md"
        if path.is_file():
            return _strip_frontmatter(path.read_text()).strip()
    searched = ", ".join(str(d) for d in style_dirs())
    print(f"WARN: no output style {name}.md found in {searched}", file=sys.stderr)
    return ""


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
        # The preset IS the previous behaviour (an unset system_prompt lets the
        # CLI use its own), so this only adds the style on top -- it replaces
        # nothing. An empty body means the tree was not found: keep the preset,
        # drop the append.
        style = style_body()
        system_prompt = {"type": "preset", "preset": "claude_code"}
        if style:
            system_prompt["append"] = style
        opts = ClaudeAgentOptions(
            model=config.model,
            max_turns=MAX_TURNS,
            permission_mode="acceptEdits",
            system_prompt=system_prompt,
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
        # On error, capture WHY: the SDK's subtype (e.g. error_max_turns,
        # error_during_execution) plus any result text. Without this a failed turn
        # is an opaque "err=True".
        error_detail = ""
        if is_error and result is not None:
            subtype = getattr(result, "subtype", None) or "unknown"
            detail = str(getattr(result, "result", "") or "")
            error_detail = f"{subtype}: {detail}".strip(": ").strip()
        return TurnResult(
            text="\n".join(texts),
            usage=usage,
            session_id=session_id,
            is_error=is_error,
            error_detail=error_detail,
        )
