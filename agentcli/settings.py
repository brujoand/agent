"""Apply the workspace's managed Claude settings values, centrally.

Fourth sibling of `skills.py`, `rules.py`, and `hooks.py`, and it exists because
the other three ship *behaviour* while a `settings.json` value is *policy* -- and
policy set by hand on one host is policy that silently drifts on every other.
`effortLevel` and `autoCompactWindow` are cost controls; a cost control you have
to remember to re-apply is not a control.

Source of truth is `settings/settings.json` in the agent checkout: Claude Code's
own schema, so whatever is valid there is valid here.

Three properties make it safe to run forever, which is the whole point:

- **Narrow.** Only declared keys are touched. Everything else in the user's file
  survives untouched -- including the host-specific values (a private OTLP
  endpoint, personal permissions) that must never reach a public repo.
- **Convergent in both directions.** The keys we applied are recorded under the
  config root, so a key *removed* from the declaration is removed from the user's
  settings on the next run. Without that record the mechanism could only ever add,
  and a retired setting would linger on every host that ever saw it.
- **Idempotent.** A second run reports `ok` for everything and rewrites nothing.

`${VAR}` in a string value is expanded from the environment at install time. An
unset variable **skips** that key rather than writing the placeholder through --
a literal `${AGENT_OTLP_ENDPOINT}` in settings.json would misconfigure telemetry
silently, which is worse than not configuring it. This is the seam that lets a
host-specific setting be declared here while its value stays out of the repo.

`hooks` is refused: `hooks.py` owns that key, and two installers writing one key
would flap. The refusal is at declaration load, so it fails loudly on the PR that
introduces it rather than quietly on someone's machine.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentcli.config import claude_config_root, repo_path
from agentcli.errors import AgentConfigError

SETTINGS_REPO = "agent"
DECLARATION = "settings.json"

# Keys another installer owns. Declaring one here would mean two writers for one
# key, so it is an error rather than a precedence puzzle.
RESERVED_KEYS = ("hooks",)

# ${VAR} only. The bare $VAR form is deliberately not supported: settings values
# contain shell-ish strings and command lines, and a bare-$ rule would expand
# things that were never meant as references.
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class Change:
    """One key's outcome. `key` is a dotted path, e.g. `env.OTEL_LOGS_EXPORTER`."""

    key: str
    outcome: str
    detail: str = ""


def source_dir() -> Path:
    """The tracked settings tree in the agent checkout -- the source of truth."""
    return repo_path(SETTINGS_REPO) / "settings"


def source_file() -> Path:
    return source_dir() / DECLARATION


def settings_file() -> Path:
    """The user-level settings this converges."""
    return claude_config_root() / "settings.json"


def state_file() -> Path:
    """Which keys we applied last time -- the record that makes removal converge.

    Under the config root rather than the cache dir so it shares the lifetime of
    the thing it describes. If it is lost we simply stop pruning: the failure mode
    is a stale key left behind, never a key deleted on a guess.
    """
    return claude_config_root() / "state" / "agent-managed-settings.json"


def declaration() -> dict[str, Any]:
    """The tracked settings, minus `_`-prefixed commentary. Validated on load."""
    path = source_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AgentConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentConfigError(f"{path}: top level must be an object")
    declared = {key: value for key, value in data.items() if not key.startswith("_")}
    for key in RESERVED_KEYS:
        if key in declared:
            raise AgentConfigError(
                f"{path}: `{key}` is owned by another installer and must not be declared here"
            )
    return declared


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Nested objects to dotted leaves, so each key is applied and tracked alone.

    Lists are leaves, not containers: a settings array (`permissions.allow`) is a
    single value with meaning as a whole, and merging one element-wise would
    produce a list nobody declared.
    """
    flat: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict) and item:
            flat.update(flatten(item, path))
        else:
            flat[path] = item
    return flat


def expand(value: Any) -> tuple[Any, list[str]]:
    """Substitute `${VAR}` in strings. Returns (value, names that were unset)."""
    if not isinstance(value, str):
        return value, []
    missing = [name for name in _VAR.findall(value) if os.environ.get(name) is None]
    if missing:
        return value, missing
    return _VAR.sub(lambda m: os.environ[m.group(1)], value), []


def _dig(target: dict[str, Any], parts: list[str]) -> dict[str, Any]:
    """The container for a dotted key, creating objects as needed."""
    node = target
    for index, part in enumerate(parts):
        existing = node.get(part)
        if existing is None:
            existing = {}
            node[part] = existing
        elif not isinstance(existing, dict):
            path = ".".join(parts[: index + 1])
            raise AgentConfigError(
                f"{settings_file()}: `{path}` is not an object but the declaration nests"
                " under it -- repair it by hand, then re-run"
            )
        node = existing
    return node


def _drop(target: dict[str, Any], parts: list[str]) -> bool:
    """Delete a dotted key, then any objects that emptied. True if it was there."""
    node = target
    chain: list[tuple[dict[str, Any], str]] = []
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            return False
        chain.append((node, part))
        node = nxt
    if parts[-1] not in node:
        return False
    del node[parts[-1]]
    for parent, part in reversed(chain):
        if parent[part] == {}:
            del parent[part]
    return True


def render(
    current: dict[str, Any], declared: dict[str, Any], previously: list[str]
) -> tuple[dict[str, Any], list[Change]]:
    """The user's settings with our keys made current. Pure; nothing on disk.

    `previously` is the keys we applied last time. One that is no longer declared
    is pruned -- that is what makes deleting a line from the declaration mean
    something.
    """
    result = json.loads(json.dumps(current))  # deep copy: never mutate the caller's
    flat = flatten(declared)
    changes: list[Change] = []

    for key in sorted(flat):
        wanted, missing = expand(flat[key])
        parts = key.split(".")
        if missing:
            # Reported, not written, and not recorded as ours: a skipped key is
            # still declared, so it is not a pruning candidate either.
            changes.append(Change(key, "skipped", f"unset: {', '.join(sorted(set(missing)))}"))
            continue
        container = _dig(result, parts[:-1])
        leaf = parts[-1]
        if leaf not in container:
            container[leaf] = wanted
            changes.append(Change(key, "set", json.dumps(wanted)))
        elif container[leaf] != wanted:
            was = container[leaf]
            container[leaf] = wanted
            changes.append(Change(key, "updated", f"{json.dumps(was)} -> {json.dumps(wanted)}"))
        else:
            changes.append(Change(key, "ok", json.dumps(wanted)))

    for key in sorted(set(previously) - set(flat)):
        if _drop(result, key.split(".")):
            changes.append(Change(key, "pruned", "no longer declared"))

    return result, changes


def applied_keys(changes: list[Change]) -> list[str]:
    """The keys now genuinely ours -- what gets recorded for the next run."""
    return sorted(c.key for c in changes if c.outcome in ("set", "updated", "ok"))


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError as exc:
        raise AgentConfigError(
            f"{path} is not valid JSON: {exc} -- repair {label} by hand"
        ) from exc
    if not isinstance(data, dict):
        raise AgentConfigError(f"{path}: top level must be an object")
    return data


def previous_keys() -> list[str]:
    """Keys recorded by the last install. Absent record means nothing to prune."""
    data = _read_json(state_file(), "the managed-settings record")
    keys = data.get("keys")
    return [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []


def _require_source() -> Path:
    path = source_file()
    if not path.is_file():
        raise AgentConfigError(
            f"no settings declaration at {path} -- run `agent pull` to fetch the agent repo first"
        )
    return path


def plan() -> list[Change]:
    """What an install would do, without doing it."""
    _require_source()
    _, changes = render(
        _read_json(settings_file(), "settings.json"), declaration(), previous_keys()
    )
    return changes


def install() -> tuple[list[Change], Path]:
    """Converge the declared keys into the user's settings. Idempotent.

    Writes only when something actually differs, so a no-op run leaves the file's
    mtime alone -- Claude Code watches it, and a rewrite for nothing is a reload
    for nothing.
    """
    _require_source()
    path = settings_file()
    current = _read_json(path, "settings.json")
    updated, changes = render(current, declaration(), previous_keys())

    if updated != current:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(updated, indent=2) + "\n")

    record = {"keys": applied_keys(changes)}
    if record != _read_json(state_file(), "the managed-settings record"):
        state_file().parent.mkdir(parents=True, exist_ok=True)
        state_file().write_text(json.dumps(record, indent=2) + "\n")

    return changes, path


def status() -> str:
    """ok | stale | missing -- whether the declared keys are already applied."""
    if not source_file().is_file():
        return "missing"
    changes = plan()
    if any(c.outcome in ("set", "updated", "pruned") for c in changes):
        return "stale"
    return "ok"


def check() -> tuple[bool, str]:
    """Doctor probe: are the managed settings applied and current?

    A skipped key is reported but does not fail: an unset variable is a host that
    has not been given that value, which is a fact about the host and not a fault
    in the settings.
    """
    path = source_file()
    if not path.is_file():
        return False, f"no settings declaration at {path} -- run `agent pull`"

    declared = flatten(declaration())
    if not declared:
        return True, f"{path}: none declared"

    changes = plan()
    skipped = [c for c in changes if c.outcome == "skipped"]
    drifted = [c for c in changes if c.outcome in ("set", "updated", "pruned")]
    note = f" ({len(skipped)} skipped: unset vars)" if skipped else ""
    if drifted:
        keys = ", ".join(c.key for c in drifted)
        return False, f"{settings_file()}: {len(drifted)} key(s) drifted -- {keys}{note}"
    return True, f"{len(declared) - len(skipped)} key(s) applied in {settings_file()}{note}"
