"""Provider protocol, neutral types, and the factory."""

import dataclasses

import pytest
from providers import AgentProvider, SessionConfig, TurnResult, TurnUsage, create_provider


def test_turn_usage_defaults_to_zero():
    usage = TurnUsage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_read_input_tokens == 0
    assert usage.cost_usd == 0.0
    assert usage.num_turns == 0


def test_neutral_types_are_frozen():
    cfg = SessionConfig(model="m", cwd="/w", session_id="s")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.model = "other"
    result = TurnResult(text="hi", usage=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.text = "other"


def test_session_config_defaults_to_fresh():
    cfg = SessionConfig(model="m", cwd="/w", session_id="s")
    assert cfg.resume is False


def test_turn_result_defaults():
    result = TurnResult(text="", usage=None)
    assert result.session_id is None
    assert result.is_error is False


def test_create_provider_claude(monkeypatch):
    import providers.claude as claude

    # Skip the boto3/env wiring: the factory must not need live MinIO creds.
    monkeypatch.setattr(claude, "make_store", lambda: object())
    provider = create_provider("claude")
    assert isinstance(provider, claude.ClaudeProvider)
    assert isinstance(provider, AgentProvider)


def test_create_provider_unknown_name_exits():
    with pytest.raises(SystemExit, match="unknown AGENT_PROVIDER 'ollama'"):
        create_provider("ollama")
