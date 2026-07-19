"""Claude adapter: SDK-message translation, options construction, resume probe.

Uses the REAL claude-agent-sdk types (dev dependency, pin synced with
issue_agent/requirements.txt) so the translation layer is tested against
genuine SDK dataclasses — only the client (which would shell out to the Claude
Code CLI) is faked, mirroring the monkeypatch idiom of test_github.py.
"""

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    project_key_for_directory,
)
from providers.base import SessionConfig
from providers.claude import (
    MAX_TURNS,
    TOOL_POLICY,
    ClaudeProvider,
    allowed_tools_for,
)


class FakeSDKClient:
    """Stands in for ClaudeSDKClient: records prompts, yields a scripted
    message stream, never touches the CLI."""

    def __init__(self, options=None):
        self.options = options
        self.prompts = []
        self.script = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        for message in self.script:
            yield message


class FakeStore:
    def __init__(self, existing=None):
        self.existing = existing
        self.load_keys = []

    async def load(self, key):
        self.load_keys.append(key)
        return self.existing


def result_message(**overrides):
    fields = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 3,
        "session_id": "sess-abc",
        "total_cost_usd": 0.25,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 9,
        },
    }
    fields.update(overrides)
    return ResultMessage(**fields)


def open_faked_session(monkeypatch, config, script):
    import providers.claude as claude

    monkeypatch.setattr(claude, "ClaudeSDKClient", FakeSDKClient)
    session = ClaudeProvider(store=FakeStore()).open_session(config)
    session._client.script = script
    return session


CFG = SessionConfig(model="claude-opus-4-8", cwd="/work", session_id="sid-1")


def test_run_turn_joins_assistant_text_blocks(monkeypatch):
    script = [
        AssistantMessage(content=[TextBlock("first")], model="m"),
        AssistantMessage(content=[TextBlock("second"), TextBlock("third")], model="m"),
        result_message(),
    ]
    session = open_faked_session(monkeypatch, CFG, script)

    async def go():
        async with session as s:
            return await s.run_turn("do the thing")

    turn = anyio.run(go)
    assert turn.text == "first\nsecond\nthird"
    assert session._client.prompts == ["do the thing"]


def test_run_turn_maps_result_message_to_usage(monkeypatch):
    session = open_faked_session(monkeypatch, CFG, [result_message()])

    turn = anyio.run(session.run_turn, "p")
    assert turn.usage is not None
    assert turn.usage.input_tokens == 100
    assert turn.usage.output_tokens == 50
    assert turn.usage.cache_creation_input_tokens == 7
    assert turn.usage.cache_read_input_tokens == 9
    assert turn.usage.cost_usd == 0.25
    assert turn.usage.num_turns == 3
    assert turn.session_id == "sess-abc"
    assert turn.is_error is False


def test_run_turn_tolerates_none_valued_usage_keys(monkeypatch):
    script = [result_message(usage={"input_tokens": None}, total_cost_usd=None)]
    session = open_faked_session(monkeypatch, CFG, script)

    turn = anyio.run(session.run_turn, "p")
    assert turn.usage.input_tokens == 0
    assert turn.usage.output_tokens == 0
    assert turn.usage.cost_usd == 0.0


def test_run_turn_without_result_message_has_no_usage(monkeypatch):
    script = [AssistantMessage(content=[TextBlock("only text")], model="m")]
    session = open_faked_session(monkeypatch, CFG, script)

    turn = anyio.run(session.run_turn, "p")
    assert turn.text == "only text"
    assert turn.usage is None
    assert turn.session_id is None
    assert turn.is_error is False


def test_run_turn_propagates_is_error(monkeypatch):
    session = open_faked_session(monkeypatch, CFG, [result_message(is_error=True)])
    turn = anyio.run(session.run_turn, "p")
    assert turn.is_error is True


def test_open_session_fresh_sets_session_id(monkeypatch):
    session = open_faked_session(monkeypatch, CFG, [])
    opts = session._client.options
    assert opts.session_id == "sid-1"
    assert opts.resume is None
    assert opts.model == "claude-opus-4-8"
    assert opts.cwd == "/work"
    assert opts.max_turns == MAX_TURNS
    assert opts.permission_mode == "acceptEdits"
    assert opts.setting_sources == ["project"]
    # CFG has the default kind ("issue") -> the full policy, incl. cluster reads.
    assert opts.allowed_tools == TOOL_POLICY["issue"]
    assert "Bash(kubectl:*)" in opts.allowed_tools


def test_open_session_pr_kind_drops_cluster_read_tools(monkeypatch):
    cfg = SessionConfig(model="m", cwd="/work", session_id="sid-1", kind="pr")
    session = open_faked_session(monkeypatch, cfg, [])
    opts = session._client.options
    assert opts.allowed_tools == TOOL_POLICY["pr"]
    # The PR agent works a checked-out diff, not the live cluster.
    assert "Bash(kubectl:*)" not in opts.allowed_tools
    assert "Bash(curl:*)" not in opts.allowed_tools
    # ...but still keeps the git/gh plumbing it needs to push and comment.
    assert "Bash(git:*)" in opts.allowed_tools
    assert "Bash(gh:*)" in opts.allowed_tools


def test_allowed_tools_for_unknown_kind_falls_back_to_issue():
    # A future/unknown role must fail open to today's broadest set, not lose tools.
    assert allowed_tools_for("something-new") == TOOL_POLICY["issue"]


def test_open_session_resume_sets_resume(monkeypatch):
    cfg = SessionConfig(model="m", cwd="/work", session_id="sid-1", resume=True)
    session = open_faked_session(monkeypatch, cfg, [])
    opts = session._client.options
    assert opts.resume == "sid-1"
    assert opts.session_id is None


def test_session_exists_probes_store_with_sdk_project_key():
    store = FakeStore(existing={"some": "transcript"})
    provider = ClaudeProvider(store=store)

    assert anyio.run(provider.session_exists, "sid-1", "/work") is True
    assert store.load_keys == [
        {"project_key": project_key_for_directory("/work"), "session_id": "sid-1"}
    ]


def test_session_exists_false_when_store_empty():
    provider = ClaudeProvider(store=FakeStore(existing=None))
    assert anyio.run(provider.session_exists, "sid-1", "/work") is False


def test_make_store_none_without_minio(monkeypatch):
    import providers.claude as claude

    # Stateless mode: no MinIO configured -> no store, so nothing to persist.
    monkeypatch.delenv("MINIO_ENDPOINT_URL", raising=False)
    assert claude.make_store() is None


def test_stateless_provider_never_resumes(monkeypatch):
    import providers.claude as claude

    # No MinIO configured -> ClaudeProvider(store=None) resolves to a stateless
    # provider (make_store() returns None).
    monkeypatch.delenv("MINIO_ENDPOINT_URL", raising=False)
    monkeypatch.setattr(claude, "ClaudeSDKClient", FakeSDKClient)
    provider = ClaudeProvider(store=None)
    assert provider._store is None

    # Reports no existing session, and opens a fresh one WITHOUT a session_store
    # even though the config asks to resume (nothing to resume from).
    assert anyio.run(provider.session_exists, "sid-1", "/work") is False

    cfg = SessionConfig(model="m", cwd="/work", session_id="sid-1", resume=True)
    opts = provider.open_session(cfg)._client.options
    assert opts.session_store is None
    assert opts.resume is None
    assert opts.session_id == "sid-1"
