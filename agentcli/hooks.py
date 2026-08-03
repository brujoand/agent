"""Distribute the workspace's shared Claude hooks to a user, centrally.

Source of truth is the `hooks/` directory in the agent checkout -- tracked,
PR-reviewed, the same tree every other repo is synced from. Third sibling of
`skills.py` and `rules.py`, and it needs one thing neither of those does.

A skill is discovered by living in `~/.claude/skills/`, and a rule by being
imported into `~/.claude/CLAUDE.md`. A hook is discovered by *neither*: Claude
Code only runs a hook that some `settings.json` names, against an event and a
matcher. So a symlinked-but-unwired hook script is a silent no-op -- installed,
inert, and indistinguishable from working until you notice the thing it was
supposed to do never happened. `install()` therefore does both halves: symlink
the scripts, then merge their declared wiring into `~/.claude/settings.json`.

The wiring lives in `hooks/hooks.json`, next to the scripts, so a hook's event
and matcher are reviewed in the same diff as its code.

Everything else mirrors `skills.py`: the links point at the checkout, so `agent
pull` updates a hook in place -- no reinstall, no drift -- and anything in
`~/.claude/hooks/` that is not one of ours is never touched. A hook placed there
by hand is a real file, not a link into this tree, so pruning skips it by
construction.

That last property has a sharp edge: a hand-placed file SHADOWS the tracked
script of the same name. The hook that fires is the untracked one, silently,
while every per-tree probe still passes. `require-worktree.sh` and
`require-fresh-branch.sh` lived that way on the reference host for weeks --
enforcing correctly, but unversioned, unreviewed, and absent from a fresh
bootstrap.

So install ADOPTS rather than skips, split by whether anything could be lost:

- bytes identical to ours -> replaced with a link, unasked. Linking loses
  nothing, and leaving it shadowed is the worse outcome.
- bytes differ -> left alone and reported, because the file carries edits we
  did not make. `--adopt` takes it, keeping the original as `<name>.bak`.

The asymmetry is the point: the common case (a copy of a script that later got
tracked) needs no flag, and the case that could destroy work needs an explicit
one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agentcli.config import claude_config_root, repo_path
from agentcli.errors import AgentConfigError

# The agent repo owns the shared hooks, for the same reason it owns the skills
# and the rules: it is already the central sync driver, and one source of truth
# beats eleven.
HOOKS_REPO = "agent"

# The declaration file is metadata about the tree, not a hook script itself.
DECLARATION = "hooks.json"


def source_dir() -> Path:
    """The tracked hooks tree in the agent checkout -- the source of truth."""
    return repo_path(HOOKS_REPO) / "hooks"


def config_root() -> Path:
    """The user's Claude config dir. Honors CLAUDE_CONFIG_DIR like Claude Code does."""
    return claude_config_root()


def dest_dir() -> Path:
    """Where per-user hook scripts live."""
    return config_root() / "hooks"


def settings_file() -> Path:
    """User-level settings: the file that decides which hooks actually run."""
    return config_root() / "settings.json"


def available() -> list[Path]:
    """Every shared hook script in the source tree."""
    src = source_dir()
    if not src.is_dir():
        return []
    return sorted(p for p in src.iterdir() if p.is_file() and p.suffix == ".sh")


def declaration() -> dict[str, list[dict[str, Any]]]:
    """The tracked event/matcher wiring, minus its `_`-prefixed commentary."""
    path = source_dir() / DECLARATION
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AgentConfigError(f"{path} is not valid JSON: {exc}") from exc
    return {event: groups for event, groups in data.items() if not event.startswith("_")}


def command_for(name: str) -> str:
    """The `command` string a settings entry uses for one hook script.

    `~`-relative when the config dir is under HOME, matching what a human would
    have written by hand and surviving a differing HOME.
    """
    path = dest_dir() / name
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _same_bytes(a: Path, b: Path) -> bool:
    """Byte-identical contents. False if either side cannot be read."""
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def status(name: str) -> str:
    """Link state of one hook script in the destination.

    ok        -- symlink resolves to our source
    missing   -- nothing there yet
    stale     -- a symlink, but pointing elsewhere (relinked on install)
    adoptable -- a real file whose bytes MATCH ours: install replaces it
    conflict  -- a real file that differs: hand-managed, needs `--adopt`

    The adoptable/conflict split exists because a real file at one of our names
    SHADOWS the tracked script silently -- the hook that fires is the untracked
    one, and every per-tree probe still passes. When the bytes are identical
    there is nothing to lose by linking, so install does it unasked. When they
    differ the file carries edits we did not make, so only an explicit
    `--adopt` (which backs it up first) may touch it.
    """
    link = dest_dir() / name
    src = source_dir() / name
    if link.is_symlink():
        return "ok" if Path(os.path.realpath(link)) == src.resolve() else "stale"
    if link.exists():
        return "adoptable" if src.is_file() and _same_bytes(link, src) else "conflict"
    return "missing"


def _require_source() -> Path:
    src = source_dir()
    if not src.is_dir():
        raise AgentConfigError(
            f"no hooks tree at {src} -- run `agent pull` to fetch the agent repo first"
        )
    return src


def _prune(dest: Path) -> list[str]:
    """Drop links to hooks that left the source tree.

    Same two guards as `skills._prune`: a link is only pruned if it points INTO
    our source tree -- a hook the user placed there themselves is never our
    business -- and only if the script is really gone.
    """
    if not dest.is_dir():
        return []
    src = source_dir().resolve()
    shared = {p.name for p in available()}
    gone = []
    for link in sorted(dest.iterdir()):
        if not link.is_symlink() or link.exists() or link.name in shared:
            continue
        target = Path(os.readlink(link))
        if target.is_absolute() and target.parent == src:
            link.unlink()
            gone.append(link.name)
    return gone


def _backup_path(link: Path) -> Path:
    """A `.bak` name beside `link` that does not already exist.

    Numbered rather than overwritten: adopting twice must never destroy the
    first backup, which may be the only copy of a hand-edited hook.
    """
    candidate = link.with_name(link.name + ".bak")
    n = 2
    while candidate.exists():
        candidate = link.with_name(f"{link.name}.bak.{n}")
        n += 1
    return candidate


def _link_all(dest: Path, adopt: bool = False) -> list[tuple[str, str]]:
    """Symlink every shared hook script into dest. Returns (name, outcome).

    `adopt` only widens the CONFLICT case (a real file whose contents differ);
    an identical file is adopted either way, since replacing it loses nothing.
    """
    results: list[tuple[str, str]] = []
    for script in available():
        name = script.name
        link = dest / name
        state = status(name)
        if state == "ok":
            results.append((name, "ok"))
        elif state == "adoptable":
            link.unlink()
            link.symlink_to(script)
            results.append((name, "adopted: replaced an identical hand-placed file"))
        elif state == "conflict":
            if not adopt:
                results.append(
                    (
                        name,
                        "SKIP: a DIFFERENT hand-placed file shadows ours -- `--adopt` to take it",
                    )
                )
                continue
            backup = _backup_path(link)
            link.rename(backup)
            link.symlink_to(script)
            results.append((name, f"adopted: hand-placed file kept at {backup.name}"))
        else:  # missing or stale -- (re)create the link
            if link.is_symlink():
                link.unlink()
            link.symlink_to(script)
            results.append((name, "relinked" if state == "stale" else "linked"))
    return results


def _is_ours(entry: Any, ours: set[str]) -> bool:
    """Does this settings entry point at a hook script we manage?

    Matched on the resolved parent directory, not the basename alone, so a
    user's own `foo.sh` somewhere else is never mistaken for ours.
    """
    if not isinstance(entry, dict) or entry.get("type") != "command":
        return False
    command = entry.get("command")
    if not isinstance(command, str):
        return False
    path = Path(os.path.expanduser(command))
    return path.name in ours and path.parent == dest_dir()


def _render_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Turn a declared entry (`script`) into a settings entry (`command`)."""
    rendered = {"type": entry.get("type", "command"), "command": command_for(entry["script"])}
    for key, value in entry.items():
        if key not in ("type", "script"):
            rendered[key] = value
    return rendered


def _group_for(groups: list[dict[str, Any]], matcher: str | None) -> dict[str, Any]:
    """The group in `groups` carrying this matcher, created at the end if absent."""
    for group in groups:
        if group.get("matcher") == matcher:
            group.setdefault("hooks", [])
            return group
    group = {"hooks": []} if matcher is None else {"matcher": matcher, "hooks": []}
    groups.append(group)
    return group


def _strip_ours(hooks: dict[str, Any], ours: set[str]) -> None:
    """Remove every entry of ours, then drop whatever that emptied."""
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                group["hooks"] = [e for e in group["hooks"] if not _is_ours(e, ours)]
        hooks[event] = [g for g in groups if not isinstance(g, dict) or g.get("hooks")]
        if not hooks[event]:
            del hooks[event]


def render_settings(settings: dict[str, Any], ours: set[str]) -> dict[str, Any]:
    """The user's settings with our wiring made current. Everything else untouched.

    Ours are stripped and re-added rather than edited in place, so a declaration
    that changed event, matcher, or timeout leaves nothing behind. `ours` covers
    scripts that just left the tree too, which is how their wiring gets dropped.
    """
    result = dict(settings)
    hooks = result.get("hooks") or {}
    if not isinstance(hooks, dict):
        raise AgentConfigError(
            f"{settings_file()}: `hooks` is not an object -- repair it by hand, then re-run"
        )
    hooks = json.loads(json.dumps(hooks))  # deep copy: never mutate the caller's dict

    _strip_ours(hooks, ours)
    for event, groups in declaration().items():
        target_groups = hooks.setdefault(event, [])
        for group in groups:
            target = _group_for(target_groups, group.get("matcher"))
            target["hooks"].extend(_render_entry(e) for e in group.get("hooks", []))

    if hooks:
        result["hooks"] = hooks
    else:
        result.pop("hooks", None)
    return result


def _read_settings() -> dict[str, Any] | None:
    """The user's settings, or None if there is no file yet."""
    path = settings_file()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AgentConfigError(
            f"{path} is not valid JSON ({exc}) -- repair it by hand, then re-run"
        ) from exc
    if not isinstance(data, dict):
        raise AgentConfigError(f"{path} is not a JSON object -- repair it by hand, then re-run")
    return data


def _serialize(settings: dict[str, Any]) -> str:
    return json.dumps(settings, indent=2) + "\n"


def _ours() -> set[str]:
    """Hook script names we manage: in the source tree, or linked from it."""
    names = {p.name for p in available()}
    dest = dest_dir()
    if not dest.is_dir():
        return names
    src = source_dir().resolve()
    for link in dest.iterdir():
        if link.is_symlink() and Path(os.readlink(link)).parent == src:
            names.add(link.name)
    return names


def settings_status() -> str:
    """State of our wiring in the user's settings file.

    ok       -- present and matching the declaration
    missing  -- no settings file yet
    stale    -- file exists, wiring absent or out of date
    """
    current = _read_settings()
    if current is None:
        return "missing"
    return "ok" if render_settings(current, _ours()) == current else "stale"


def install(adopt: bool = False) -> tuple[list[tuple[str, str]], str, Path]:
    """Symlink the shared hooks and wire them into settings. Idempotent.

    Returns (link results, settings outcome, settings path). Only ever adds,
    updates, or removes settings entries that point into the hooks dir at a
    script of ours.

    A hand-placed real file at one of our names is replaced when its bytes are
    identical to ours (nothing is lost) and otherwise left alone -- pass
    `adopt=True` to take that one too, keeping the original as a `.bak`.
    """
    _require_source()
    dest = dest_dir()
    dest.mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, str]] = [(name, "pruned") for name in _prune(dest)]
    ours = _ours() | {name for name, _ in results}
    results.extend(_link_all(dest, adopt=adopt))

    current = _read_settings()
    desired = render_settings(current or {}, ours)
    if current == desired:
        return results, "ok", settings_file()
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_serialize(desired))
    return results, "created" if current is None else "updated", path


def check() -> tuple[bool, str]:
    """Doctor probe: are the shared hooks linked and wired?

    Not-yet-installed is a clean, expected state, so it is reported without
    failing -- `agent hooks install` is the fix, not a repair.
    """
    src = source_dir()
    if not src.is_dir():
        return False, f"no hooks tree at {src} -- run `agent pull`"

    scripts = available()
    if not scripts:
        return True, f"{src}: none defined"

    states = {p.name: status(p.name) for p in scripts}
    broken = {n: st for n, st in states.items() if st in ("stale", "adoptable", "conflict")}
    if broken:
        detail = ", ".join(f"{n}: {st}" for n, st in sorted(broken.items()))
        # A conflict is the one state plain install cannot clear, so name the
        # flag that does rather than sending the reader round the loop again.
        fix = (
            "agent hooks install --adopt"
            if "conflict" in broken.values()
            else "agent hooks install"
        )
        return False, f"{detail} -- run `{fix}`"

    linked = sum(1 for st in states.values() if st == "ok")
    wiring = settings_status()
    if linked < len(scripts) or wiring != "ok":
        # An unwired hook never fires, so say so rather than counting links.
        return True, f"{linked}/{len(scripts)} linked, wiring {wiring} -- run `agent hooks install`"
    return True, f"{linked} linked into {dest_dir()}, wired in {settings_file()}"
