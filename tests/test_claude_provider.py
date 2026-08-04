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
    style_body,
    style_dirs,
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


# --- output style: the same file the interactive setup symlinks ---------------


def test_style_body_strips_frontmatter_and_keeps_the_instructions():
    body = style_body()
    assert body, "the repo's output-styles tree should be found from a checkout"
    assert not body.startswith("---")
    assert "keep-coding-instructions" not in body
    assert "## Register" in body


def test_style_dirs_cover_the_image_and_a_checkout():
    names = [str(d) for d in style_dirs()]
    # /opt/issue-agent/output-styles in the image, <repo>/output-styles locally.
    assert names[0].endswith("issue_agent/output-styles")
    assert names[1].endswith("agent/output-styles") or names[1].endswith("output-styles")


def test_open_session_appends_the_style_to_the_claude_code_preset(monkeypatch):
    session = open_faked_session(monkeypatch, CFG, [])
    prompt = session._client.options.system_prompt
    assert prompt["type"] == "preset"
    assert prompt["preset"] == "claude_code"
    assert "## Register" in prompt["append"]


def test_open_session_keeps_the_preset_when_no_style_is_found(monkeypatch, tmp_path, capsys):
    import providers.claude as claude

    monkeypatch.setattr(claude, "style_dirs", lambda: [tmp_path / "nowhere"])
    session = open_faked_session(monkeypatch, CFG, [])
    assert session._client.options.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert "WARN: no output style" in capsys.readouterr().err


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


def test_run_turn_captures_error_detail(monkeypatch):
    # On error, the reason (subtype + message) is captured so a failed run is not
    # an opaque "err=True".
    script = [result_message(is_error=True, subtype="error_during_execution", result="boom")]
    session = open_faked_session(monkeypatch, CFG, script)
    turn = anyio.run(session.run_turn, "p")
    assert turn.is_error is True
    assert "error_during_execution" in turn.error_detail
    assert "boom" in turn.error_detail


def test_run_turn_no_error_detail_on_success(monkeypatch):
    session = open_faked_session(monkeypatch, CFG, [result_message()])
    turn = anyio.run(session.run_turn, "p")
    assert turn.is_error is False
    assert turn.error_detail == ""


def test_open_session_fresh_sets_session_id(monkeypatch):
    monkeypatch.delenv("AGENT_CLUSTER_TOOLS", raising=False)
    session = open_faked_session(monkeypatch, CFG, [])
    opts = session._client.options
    assert opts.session_id == "sid-1"
    assert opts.resume is None
    assert opts.model == "claude-opus-4-8"
    assert opts.cwd == "/work"
    assert opts.max_turns == MAX_TURNS
    assert opts.permission_mode == "acceptEdits"
    assert opts.setting_sources == ["project"]
    # Secure by default: an issue session gets NO cluster reads unless opted in.
    assert opts.allowed_tools == TOOL_POLICY["issue"]
    assert "Bash(kubectl:*)" not in opts.allowed_tools
    assert "Bash(curl:*)" not in opts.allowed_tools


def test_issue_gets_cluster_tools_only_when_opted_in(monkeypatch):
    # A deployment's own privileged workflow sets AGENT_CLUSTER_TOOLS=1; every
    # other run, which never sets it, stays locked down.
    monkeypatch.setenv("AGENT_CLUSTER_TOOLS", "1")
    assert "Bash(kubectl:*)" in allowed_tools_for("issue")
    assert "Bash(curl:*)" in allowed_tools_for("issue")
    # Even opted in, the PR role never gets cluster reads.
    assert "Bash(kubectl:*)" not in allowed_tools_for("pr")
    monkeypatch.delenv("AGENT_CLUSTER_TOOLS", raising=False)
    assert "Bash(kubectl:*)" not in allowed_tools_for("issue")


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


# --- the PreToolUse guard is wired, and denies in the shape the SDK expects ---


def test_options_carry_deny_rules_and_the_guard(monkeypatch):
    import tool_policy

    session = open_faked_session(monkeypatch, CFG, [])
    opts = session._client.options

    assert opts.disallowed_tools == tool_policy.DENIED_TOOLS
    # A deny rule alone is a permission gate, not a sandbox -- the hook is the
    # half that holds, so its absence must fail the build.
    assert "PreToolUse" in opts.hooks
    assert opts.hooks["PreToolUse"][0].hooks, "no PreToolUse callback registered"


def test_guard_denies_with_the_sdk_permission_shape():
    from providers.claude import make_guard

    guard = make_guard("/work")
    out = anyio.run(
        guard, {"tool_name": "Bash", "tool_input": {"command": "gh auth token"}}, "id", None
    )

    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert "token" in specific["permissionDecisionReason"]


def test_guard_allows_ordinary_work():
    from providers.claude import make_guard

    guard = make_guard("/work")
    out = anyio.run(
        guard, {"tool_name": "Bash", "tool_input": {"command": "git status"}}, "id", None
    )
    assert out == {}


def test_guard_fails_closed_when_the_policy_errors(monkeypatch):
    # This guard defends a security boundary, unlike the shell hooks that fail
    # open so a bug never wedges a session. An internal error must DENY.
    import providers.claude as claude

    def boom(*a, **k):
        raise RuntimeError("policy exploded")

    monkeypatch.setattr(claude.tool_policy, "denial_reason", boom)
    guard = claude.make_guard("/work")
    out = anyio.run(
        guard, {"tool_name": "Bash", "tool_input": {"command": "git status"}}, "id", None
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- the spend ceiling -------------------------------------------------------


def test_options_carry_the_spend_ceiling(monkeypatch):
    from providers.claude import DEFAULT_MAX_BUDGET_USD

    monkeypatch.delenv("AGENT_MAX_BUDGET_USD", raising=False)
    opts = open_faked_session(monkeypatch, CFG, [])._client.options
    assert opts.max_budget_usd == DEFAULT_MAX_BUDGET_USD


def test_spend_ceiling_is_configurable(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_BUDGET_USD", "2.5")
    opts = open_faked_session(monkeypatch, CFG, [])._client.options
    assert opts.max_budget_usd == 2.5


def test_spend_ceiling_can_be_disabled(monkeypatch):
    # An explicit 0 means "bound cost somewhere else", not "use the default".
    monkeypatch.setenv("AGENT_MAX_BUDGET_USD", "0")
    opts = open_faked_session(monkeypatch, CFG, [])._client.options
    assert opts.max_budget_usd is None


def test_unparseable_ceiling_falls_back_rather_than_uncapping(monkeypatch):
    from providers.claude import DEFAULT_MAX_BUDGET_USD, max_budget_usd

    monkeypatch.setenv("AGENT_MAX_BUDGET_USD", "ten dollars")
    assert max_budget_usd() == DEFAULT_MAX_BUDGET_USD


def test_turn_carries_the_raw_subtype(monkeypatch):
    # The wrapper branches on this, so it must arrive unformatted -- rewording
    # the human-readable detail must not change control flow.
    session = open_faked_session(
        monkeypatch,
        CFG,
        [result_message(is_error=True, subtype="error_max_budget_usd", result="over budget")],
    )
    turn = anyio.run(session.run_turn, "p")

    assert turn.subtype == "error_max_budget_usd"
    assert turn.hit_cost_ceiling() is True
    assert "error_max_budget_usd" in turn.error_detail


def test_an_ordinary_error_is_not_a_cost_ceiling(monkeypatch):
    session = open_faked_session(
        monkeypatch,
        CFG,
        [result_message(is_error=True, subtype="error_during_execution", result="nope")],
    )
    turn = anyio.run(session.run_turn, "p")

    assert turn.subtype == "error_during_execution"
    assert turn.hit_cost_ceiling() is False


def test_a_successful_turn_has_no_subtype(monkeypatch):
    session = open_faked_session(monkeypatch, CFG, [result_message()])
    turn = anyio.run(session.run_turn, "p")
    assert turn.subtype == ""
    assert turn.hit_cost_ceiling() is False
