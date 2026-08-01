from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentcli import settings
from agentcli.errors import AgentConfigError


@pytest.fixture
def tree(monkeypatch, tmp_path):
    """A fake declaration and a config root, wired into the module."""
    src = tmp_path / "src" / "settings"
    src.mkdir(parents=True)
    config = tmp_path / "cfg"
    config.mkdir()
    monkeypatch.setattr(settings, "source_dir", lambda: src)
    monkeypatch.setattr(settings, "source_file", lambda: src / "settings.json")
    monkeypatch.setattr(settings, "settings_file", lambda: config / "settings.json")
    monkeypatch.setattr(settings, "state_file", lambda: config / "state" / "managed.json")
    return src, config


def _declare(src: Path, data: dict) -> None:
    (src / "settings.json").write_text(json.dumps(data))


def _user(config: Path, data: dict) -> None:
    (config / "settings.json").write_text(json.dumps(data))


def _read_user(config: Path) -> dict:
    return json.loads((config / "settings.json").read_text())


def _outcomes(changes: list[settings.Change]) -> dict[str, str]:
    return {c.key: c.outcome for c in changes}


# --- declaration loading ------------------------------------------------------


def test_declaration_strips_commentary(tree):
    src, _ = tree
    _declare(src, {"_comment": ["why"], "effortLevel": "high"})
    assert settings.declaration() == {"effortLevel": "high"}


def test_declaration_refuses_a_key_another_installer_owns(tree):
    src, _ = tree
    _declare(src, {"hooks": {"UserPromptSubmit": []}})
    with pytest.raises(AgentConfigError, match="owned by another installer"):
        settings.declaration()


def test_declaration_rejects_malformed_json(tree):
    src, _ = tree
    (src / "settings.json").write_text("{not json")
    with pytest.raises(AgentConfigError, match="not valid JSON"):
        settings.declaration()


def test_install_without_a_declaration_is_an_error(tree):
    with pytest.raises(AgentConfigError, match="agent pull"):
        settings.install()


# --- flattening and expansion -------------------------------------------------


def test_flatten_treats_lists_as_leaves():
    flat = settings.flatten({"a": 1, "env": {"X": "1", "Y": "2"}, "permissions": {"allow": ["a"]}})
    assert flat == {"a": 1, "env.X": "1", "env.Y": "2", "permissions.allow": ["a"]}


def test_expand_substitutes_set_variables(monkeypatch):
    monkeypatch.setenv("AGENT_TEST_HOST", "metrics.example.com")
    value, missing = settings.expand("https://${AGENT_TEST_HOST}/v1/otlp")
    assert (value, missing) == ("https://metrics.example.com/v1/otlp", [])


def test_expand_reports_unset_variables_without_substituting(monkeypatch):
    monkeypatch.delenv("AGENT_TEST_ABSENT", raising=False)
    value, missing = settings.expand("https://${AGENT_TEST_ABSENT}/v1")
    assert missing == ["AGENT_TEST_ABSENT"]
    assert value == "https://${AGENT_TEST_ABSENT}/v1"


def test_expand_ignores_bare_dollar_names(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    # A settings value may legitimately contain `$HOME` as literal text.
    assert settings.expand("echo $HOME") == ("echo $HOME", [])


# --- render -------------------------------------------------------------------


def test_render_sets_updates_and_confirms(tree):
    _, _ = tree
    current = {"effortLevel": "xhigh", "theme": "auto"}
    declared = {"effortLevel": "high", "autoCompactWindow": 250_000, "theme": "auto"}

    result, changes = settings.render(current, declared, previously=[])

    assert result == {"effortLevel": "high", "theme": "auto", "autoCompactWindow": 250_000}
    assert _outcomes(changes) == {
        "effortLevel": "updated",
        "autoCompactWindow": "set",
        "theme": "ok",
    }


def test_render_never_mutates_the_caller(tree):
    current = {"env": {"KEEP": "1"}}
    settings.render(current, {"env": {"NEW": "2"}}, previously=[])
    assert current == {"env": {"KEEP": "1"}}


def test_render_leaves_undeclared_keys_alone(tree):
    current = {
        "env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://private.example/v1"},
        "permissions": {"allow": ["Bash(git *)"]},
        "hooks": {"UserPromptSubmit": [{"hooks": []}]},
    }
    result, _ = settings.render(current, {"effortLevel": "high"}, previously=[])
    assert result["env"] == current["env"]
    assert result["permissions"] == current["permissions"]
    assert result["hooks"] == current["hooks"]


def test_render_merges_into_a_nested_object_without_clobbering_siblings(tree):
    current = {"env": {"KEEP_ME": "yes"}}
    result, changes = settings.render(current, {"env": {"ADDED": "1"}}, previously=[])
    assert result["env"] == {"KEEP_ME": "yes", "ADDED": "1"}
    assert _outcomes(changes) == {"env.ADDED": "set"}


def test_render_skips_a_key_whose_variable_is_unset(tree, monkeypatch):
    monkeypatch.delenv("AGENT_TEST_ABSENT", raising=False)
    declared = {"env": {"ENDPOINT": "${AGENT_TEST_ABSENT}"}, "effortLevel": "high"}

    result, changes = settings.render({}, declared, previously=[])

    # Skipped means absent, not written with the placeholder left in.
    assert "env" not in result
    assert result == {"effortLevel": "high"}
    assert _outcomes(changes)["env.ENDPOINT"] == "skipped"


def test_skipped_keys_are_not_recorded_as_ours(tree, monkeypatch):
    monkeypatch.delenv("AGENT_TEST_ABSENT", raising=False)
    _, changes = settings.render({}, {"env": {"E": "${AGENT_TEST_ABSENT}"}}, previously=[])
    assert settings.applied_keys(changes) == []


def test_render_prunes_a_key_that_left_the_declaration(tree):
    current = {"effortLevel": "high", "autoCompactWindow": 250_000}

    result, changes = settings.render(
        current, {"effortLevel": "high"}, previously=["effortLevel", "autoCompactWindow"]
    )

    assert result == {"effortLevel": "high"}
    assert _outcomes(changes)["autoCompactWindow"] == "pruned"


def test_render_pruning_cleans_up_an_emptied_parent(tree):
    current = {"env": {"GONE": "1"}, "effortLevel": "high"}
    result, changes = settings.render(current, {"effortLevel": "high"}, previously=["env.GONE"])
    assert result == {"effortLevel": "high"}
    assert _outcomes(changes)["env.GONE"] == "pruned"


def test_render_never_prunes_a_key_it_did_not_apply(tree):
    # The user's own value, never recorded as ours -- pruning must not reach it.
    current = {"theme": "dark", "effortLevel": "high"}
    result, _ = settings.render(current, {"effortLevel": "high"}, previously=[])
    assert result["theme"] == "dark"


def test_render_rejects_a_scalar_where_the_declaration_nests(tree):
    with pytest.raises(AgentConfigError, match="not an object"):
        settings.render({"env": "oops"}, {"env": {"X": "1"}}, previously=[])


# --- install ------------------------------------------------------------------


def test_install_creates_the_file_and_records_its_keys(tree):
    src, config = tree
    _declare(src, {"effortLevel": "high", "autoCompactWindow": 250_000})

    changes, path = settings.install()

    assert path == config / "settings.json"
    assert _read_user(config) == {"effortLevel": "high", "autoCompactWindow": 250_000}
    assert sorted(_outcomes(changes)) == ["autoCompactWindow", "effortLevel"]
    assert settings.previous_keys() == ["autoCompactWindow", "effortLevel"]


def test_install_is_idempotent_and_does_not_rewrite_the_file(tree):
    src, config = tree
    _declare(src, {"effortLevel": "high"})
    settings.install()
    stamp = (config / "settings.json").stat().st_mtime_ns

    changes, _ = settings.install()

    assert _outcomes(changes) == {"effortLevel": "ok"}
    # Claude Code watches this file; a rewrite for nothing is a reload for nothing.
    assert (config / "settings.json").stat().st_mtime_ns == stamp


def test_install_preserves_everything_it_does_not_declare(tree):
    src, config = tree
    _user(config, {"env": {"SECRET_ENDPOINT": "https://private.example"}, "theme": "auto"})
    _declare(src, {"effortLevel": "high"})

    settings.install()

    after = _read_user(config)
    assert after["env"] == {"SECRET_ENDPOINT": "https://private.example"}
    assert after["theme"] == "auto"
    assert after["effortLevel"] == "high"


def test_install_converges_after_a_key_is_removed_from_the_declaration(tree):
    src, config = tree
    _declare(src, {"effortLevel": "high", "autoCompactWindow": 250_000})
    settings.install()

    _declare(src, {"effortLevel": "high"})
    changes, _ = settings.install()

    assert _read_user(config) == {"effortLevel": "high"}
    assert _outcomes(changes)["autoCompactWindow"] == "pruned"
    assert settings.previous_keys() == ["effortLevel"]
    # And the prune is not re-reported forever.
    assert "autoCompactWindow" not in _outcomes(settings.install()[0])


def test_install_reapplies_a_value_a_human_changed_by_hand(tree):
    src, config = tree
    _declare(src, {"autoCompactWindow": 250_000})
    settings.install()
    _user(config, {"autoCompactWindow": 900_000})

    changes, _ = settings.install()

    assert _read_user(config)["autoCompactWindow"] == 250_000
    assert _outcomes(changes)["autoCompactWindow"] == "updated"


def test_install_rejects_a_malformed_user_settings_file(tree):
    src, config = tree
    _declare(src, {"effortLevel": "high"})
    (config / "settings.json").write_text("{not json")
    with pytest.raises(AgentConfigError, match="repair settings.json by hand"):
        settings.install()


def test_install_survives_a_lost_state_record_without_deleting_anything(tree):
    src, config = tree
    _declare(src, {"effortLevel": "high", "autoCompactWindow": 250_000})
    settings.install()
    (config / "state" / "managed.json").unlink()

    _declare(src, {"effortLevel": "high"})
    settings.install()

    # No record means nothing is known to be ours, so the undeclared key stays.
    assert _read_user(config)["autoCompactWindow"] == 250_000


# --- status and doctor --------------------------------------------------------


def test_status_and_check_report_drift_then_agreement(tree):
    src, config = tree
    _declare(src, {"effortLevel": "high"})

    assert settings.status() == "stale"
    ok, detail = settings.check()
    assert not ok
    assert "drifted" in detail and "effortLevel" in detail

    settings.install()

    assert settings.status() == "ok"
    ok, detail = settings.check()
    assert ok
    assert str(config / "settings.json") in detail


def test_check_counts_a_skipped_key_without_failing(tree, monkeypatch):
    src, _ = tree
    monkeypatch.delenv("AGENT_TEST_ABSENT", raising=False)
    _declare(src, {"env": {"E": "${AGENT_TEST_ABSENT}"}})

    settings.install()
    ok, detail = settings.check()

    # An unset variable is a fact about the host, not a fault in the settings.
    assert ok
    assert "skipped" in detail


def test_status_is_missing_without_a_declaration(tree):
    assert settings.status() == "missing"
    ok, detail = settings.check()
    assert not ok
    assert "agent pull" in detail


def test_plan_does_not_touch_disk(tree):
    src, config = tree
    _declare(src, {"effortLevel": "high"})
    assert _outcomes(settings.plan()) == {"effortLevel": "set"}
    assert not (config / "settings.json").exists()


# --- the shipped declaration --------------------------------------------------


REAL_DECLARATION = Path(__file__).resolve().parent.parent / "settings" / "settings.json"


def test_shipped_declaration_is_valid_and_declares_the_cost_controls():
    data = json.loads(REAL_DECLARATION.read_text())
    declared = {k: v for k, v in data.items() if not k.startswith("_")}

    assert declared["effortLevel"] in ("low", "medium", "high", "xhigh")
    assert declared["autoCompactEnabled"] is True
    assert declared["autoCompactWindow"] > 0
    assert not set(declared) & set(settings.RESERVED_KEYS)


def test_the_hook_fallback_budget_tracks_the_declared_window():
    """The hook's fallback and the declared window must agree, or a host that has
    not applied the setting yet is warned against a threshold that is not the
    real one. Asserted as agreement between the two files rather than against a
    literal, so tuning the window stays a one-line change."""
    declared = json.loads(REAL_DECLARATION.read_text())["autoCompactWindow"]
    hook = (REAL_DECLARATION.parent.parent / "hooks" / "context-budget.sh").read_text()
    match = re.search(r"^readonly DEFAULT_BUDGET=(\d+)$", hook, re.MULTILINE)
    assert match, "context-budget.sh no longer declares DEFAULT_BUDGET"
    assert int(match.group(1)) == declared


def test_shipped_declaration_carries_no_host_specific_values():
    """This repo is public: a host-specific value belongs behind `${VAR}`.

    Asserted structurally rather than against a denylist of real hostnames and
    subnets -- writing those literals into a public repo is the very leak the rule
    exists to prevent, so the concrete list stays in local (uncommitted) memory.
    Every declared string must be a bare literal or a `${VAR}` reference; a URL or
    an IP address is neither.
    """
    declared = json.loads(REAL_DECLARATION.read_text())
    for key, value in settings.flatten(declared).items():
        if key.startswith("_") or not isinstance(value, str):
            continue
        stripped = settings._VAR.sub("", value)
        assert "://" not in stripped, f"{key}: a URL must come from the environment"
        assert not re.fullmatch(r"[\d.:]+", stripped), f"{key}: an address must come from the env"


REAL_SETTINGS_README = Path(__file__).resolve().parent.parent / "settings" / "README.md"


def test_every_host_supplied_variable_is_documented():
    """A `${VAR}` nobody documents is a key that silently never applies.

    That is not hypothetical: the telemetry block lived un-declared and
    hand-copied per host, and the resulting gap went unnoticed for a month
    because an exporter that was never configured looks exactly like a quiet
    week. The mechanism reports a skipped key, but only someone who knows the
    variable exists can act on the report -- so the README is the other half of
    the mechanism, and this keeps the two from drifting apart.
    """
    declared = json.loads(REAL_DECLARATION.read_text())
    readme = REAL_SETTINGS_README.read_text()

    referenced = {
        name
        for key, value in settings.flatten(declared).items()
        if not key.startswith("_") and isinstance(value, str)
        for name in settings._VAR.findall(value)
    }
    assert referenced, "expected the declaration to use the ${VAR} seam"
    for name in sorted(referenced):
        assert name in readme, f"{name} is referenced by the declaration but undocumented"
