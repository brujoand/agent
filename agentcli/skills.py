"""Distribute the workspace's shared Claude skills to a user, centrally.

Source of truth is the `skills/` directory in the agent checkout -- tracked,
PR-reviewed, the same tree every other repo is synced from. `agent skills
install` symlinks each skill *directory* into the user's Claude config
(`~/.claude/skills/`), so:

- one install per user covers every repo AND every worktree (they read the same
  `~/.claude/skills/`), and
- the link points at the checkout, so `agent pull` fast-forwarding the agent repo
  updates the skill in place -- no reinstall, no drift.

This mirrors `install.py`'s lab symlink: the source is the checkout under the
agent root (`repo_path("agent")`), never the installed package copy under
`~/.local/share/uv/tools/...`, which only moves when the CLI is reinstalled.
"""

from __future__ import annotations

import os
from pathlib import Path

from agentcli.config import repo_path
from agentcli.errors import AgentConfigError

# The agent repo owns the shared skills. `agent` is already the central sync
# driver (pull, tokens, worktrees); shipping the skills from the same checkout
# keeps one source of truth.
SKILLS_REPO = "agent"


def source_dir() -> Path:
    """The tracked skills tree in the agent checkout -- the source of truth."""
    return repo_path(SKILLS_REPO) / "skills"


def dest_dir() -> Path:
    """Where per-user skills live. Honors CLAUDE_CONFIG_DIR like Claude Code does."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".claude"
    return root / "skills"


def available() -> list[Path]:
    """Every shared skill directory (one holding a SKILL.md) in the source tree."""
    src = source_dir()
    if not src.is_dir():
        return []
    return sorted(d for d in src.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())


def _require_source() -> Path:
    src = source_dir()
    if not src.is_dir():
        raise AgentConfigError(
            f"no skills tree at {src} -- run `agent pull` to fetch the agent repo first"
        )
    return src


def status(name: str) -> str:
    """Link state of one skill in the destination.

    ok        -- symlink resolves to our source
    missing   -- nothing there yet
    stale     -- a symlink, but pointing elsewhere (relinked on install)
    conflict  -- a real file/dir, not a symlink: a hand-managed skill we won't touch
    """
    link = dest_dir() / name
    src = source_dir() / name
    if link.is_symlink():
        target = Path(os.path.realpath(link))
        return "ok" if target == src.resolve() else "stale"
    if link.exists():
        return "conflict"
    return "missing"


def _prune(dest: Path) -> list[str]:
    """Drop links to skills that left the source tree (renamed, or promoted to a rule).

    Two guards. A link is only pruned if it points INTO our source tree -- a
    user's own skill, or a link they made elsewhere, is never our business -- and
    only if the skill is really gone: a dangling link to a skill still in the
    tree is a *stale* link, which the install loop relinks. `install` is the only
    command that writes to dest, so this belongs here.
    """
    if not dest.is_dir():
        return []
    src = source_dir().resolve()
    shared = {s.name for s in available()}
    gone = []
    for link in sorted(dest.iterdir()):
        if not link.is_symlink() or link.exists() or link.name in shared:
            continue
        target = Path(os.readlink(link))
        if target.is_absolute() and target.parent == src:
            link.unlink()
            gone.append(link.name)
    return gone


def install() -> list[tuple[str, str]]:
    """Symlink every shared skill into the user's Claude skills dir. Idempotent.

    Never clobbers a real directory the user placed there themselves (that is a
    'conflict' -- reported, left alone). Returns (name, outcome) per skill.
    """
    _require_source()
    dest = dest_dir()
    dest.mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, str]] = [(name, "pruned") for name in _prune(dest)]
    for skill in available():
        name = skill.name
        link = dest / name
        state = status(name)
        if state == "ok":
            results.append((name, "ok"))
        elif state == "conflict":
            results.append((name, "SKIP: a non-symlink already exists here"))
        else:  # missing or stale -- (re)create the link
            if link.is_symlink():
                link.unlink()
            link.symlink_to(skill)
            results.append((name, "relinked" if state == "stale" else "linked"))
    return results


def check() -> tuple[bool, str]:
    """Doctor probe: are the shared skills installed and pointing at the source?

    Returns (ok, detail). Not-yet-installed is a clean, expected state, so it is
    reported without failing -- `agent skills install` is the fix, not a repair.
    """
    src = source_dir()
    if not src.is_dir():
        return False, f"no skills tree at {src} -- run `agent pull`"

    skills = available()
    if not skills:
        return True, f"{src}: none defined"

    states = {s.name: status(s.name) for s in skills}
    broken = {n: st for n, st in states.items() if st in ("stale", "conflict")}
    if broken:
        detail = ", ".join(f"{n}: {st}" for n, st in sorted(broken.items()))
        return False, f"{detail} -- run `agent skills install`"

    linked = sum(1 for st in states.values() if st == "ok")
    if linked < len(skills):
        return True, f"{linked}/{len(skills)} linked -- run `agent skills install` for the rest"
    return True, f"{linked} linked into {dest_dir()}"
