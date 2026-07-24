"""Distribute the workspace's always-on Claude rules to a user, centrally.

Source of truth is the `rules/` directory in the agent checkout -- tracked,
PR-reviewed, the same tree every other repo is synced from. Sibling of
`skills.py`, and the split between them is the point:

- a *skill* is opt-in. Claude sees its name and description and decides whether
  to load the body. That is right for a task procedure, and wrong for house
  style, which has to be in context BEFORE the first response, every time.
- a *rule* is always on. Claude Code reads `~/.claude/CLAUDE.md` at the start of
  every session in every directory -- worktrees under `~/worktrees/` included,
  which is exactly where `~/src/CLAUDE.md` stops applying.

So `agent rules install` writes a marked block of `@`-imports into the user's
`~/.claude/CLAUDE.md`, one per tracked rule file. The imports point at the
checkout, so `agent pull` fast-forwarding the agent repo updates the rules in
place -- no reinstall, no drift, same property the skill symlinks have.

Anything the user wrote outside the markers is left alone.
"""

from __future__ import annotations

import os
from pathlib import Path

from agentcli.config import repo_path
from agentcli.errors import AgentConfigError

# The agent repo owns the shared rules, for the same reason it owns the skills:
# it is already the central sync driver, and one source of truth beats eleven.
RULES_REPO = "agent"

START = "<!-- agent:rules:start -- managed by `agent rules install`, do not edit -->"
END = "<!-- agent:rules:end -->"

_HEADER = "# Claude memory (managed)"


def source_dir() -> Path:
    """The tracked rules tree in the agent checkout -- the source of truth."""
    return repo_path(RULES_REPO) / "rules"


def config_root() -> Path:
    """The user's Claude config dir. Honors CLAUDE_CONFIG_DIR like Claude Code does."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base).expanduser() if base else Path.home() / ".claude"


def memory_file() -> Path:
    """User-level memory: the one file Claude Code loads in every session, everywhere."""
    return config_root() / "CLAUDE.md"


def available() -> list[Path]:
    """Every tracked rule file, in the order they will be imported."""
    src = source_dir()
    if not src.is_dir():
        return []
    return sorted(p for p in src.iterdir() if p.is_file() and p.suffix == ".md")


def _import_path(rule: Path) -> str:
    """`~`-relative when under HOME, so the block survives a differing HOME."""
    try:
        return f"~/{rule.relative_to(Path.home())}"
    except ValueError:
        return str(rule)


def block() -> str:
    """The managed block: markers around one `@` import per rule file."""
    imports = "\n".join(f"@{_import_path(rule)}" for rule in available())
    return f"{START}\n{imports}\n{END}" if imports else f"{START}\n{END}"


def _require_source() -> Path:
    src = source_dir()
    if not src.is_dir():
        raise AgentConfigError(
            f"no rules tree at {src} -- run `agent pull` to fetch the agent repo first"
        )
    return src


def _split(text: str) -> tuple[str, str] | None:
    """Text before and after the managed block, or None if it is not there yet."""
    if START not in text:
        return None
    before, _, rest = text.partition(START)
    if END not in rest:
        raise AgentConfigError(
            f"{memory_file()} has an opening rules marker but no closing one -- "
            "repair it by hand, then re-run"
        )
    _, _, after = rest.partition(END)
    return before, after


def render(text: str | None) -> str:
    """The memory file's new contents: managed block in, everything else untouched."""
    current = block()
    if text is None or not text.strip():
        return f"{_HEADER}\n\n{current}\n"
    parts = _split(text)
    if parts is None:  # a hand-written memory file: keep it, append the block
        return f"{text.rstrip()}\n\n{current}\n"
    before, after = parts
    return f"{before}{current}{after}"


def status() -> str:
    """State of the managed block in the user's memory file.

    ok       -- present and matching the tracked rules
    missing  -- no memory file yet
    absent   -- memory file exists but carries no managed block
    stale    -- block present, contents differ (a rule was added, renamed, dropped)
    """
    path = memory_file()
    if not path.is_file():
        return "missing"
    text = path.read_text()
    if START not in text:
        return "absent"
    return "ok" if render(text) == text else "stale"


def install() -> tuple[str, Path]:
    """Write the managed import block into ~/.claude/CLAUDE.md. Idempotent.

    Returns (outcome, path). Never touches a line outside the markers.
    """
    _require_source()
    path = memory_file()
    state = status()
    if state == "ok":
        return "ok", path
    text = path.read_text() if path.is_file() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(text))
    outcome = {"missing": "created", "absent": "added", "stale": "updated"}[state]
    return outcome, path


def check() -> tuple[bool, str]:
    """Doctor probe: is the rules block installed and current?

    Not-yet-installed is a clean, expected state, so it is reported without
    failing -- `agent rules install` is the fix, not a repair.
    """
    src = source_dir()
    if not src.is_dir():
        return False, f"no rules tree at {src} -- run `agent pull`"

    rules = available()
    if not rules:
        return True, f"{src}: none defined"

    names = ", ".join(r.stem for r in rules)
    state = status()
    if state == "ok":
        return True, f"{len(rules)} rule(s) imported by {memory_file()}: {names}"
    if state == "stale":
        return False, f"{memory_file()}: block is out of date -- run `agent rules install`"
    return True, f"{len(rules)} rule(s) not installed -- run `agent rules install` ({names})"
