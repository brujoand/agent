"""Distribute the workspace's shared Claude output styles to a user, centrally.

Source of truth is the `output-styles/` directory in the agent checkout --
tracked, PR-reviewed, the same tree every other repo is synced from. Fifth
sibling of `skills.py`, `rules.py`, `hooks.py` and `settings.py`, and the reason
it is its own tree rather than a rule is *where the text lands*.

A rule is user memory: it arrives as content, in the same channel as everything
else the model reads, and it competes with the task for attention. An output
style is different -- Claude Code splices it into the **system prompt**, so it
shapes register before the first token of the conversation exists. House style
belongs there. `keep-coding-instructions: true` in a style's frontmatter keeps
the default coding instructions alongside it, so a style tunes voice without
throwing away the harness.

Mechanically this is `skills.py` over files instead of directories: symlink each
`<name>.md` into `~/.claude/output-styles/`, point the link at the checkout so
`agent pull` updates the style in place, and never touch anything that is not
ours. Installing a style does not *select* it -- the active one is the
`outputStyle` key in settings.json, which `settings.py` converges. Two halves,
same split as hooks: this tree ships the style, the settings declaration decides
which one is on.
"""

from __future__ import annotations

import os
from pathlib import Path

from agentcli.config import claude_config_root, repo_path
from agentcli.errors import AgentConfigError

# The agent repo owns the shared output styles, for the same reason it owns the
# skills, the rules and the hooks: it is already the central sync driver, and one
# source of truth beats eleven.
STYLES_REPO = "agent"

# Prose about the tree, not a style in it. Claude Code would happily offer a
# `README` style in the picker otherwise.
IGNORED = frozenset({"README.md"})


def source_dir() -> Path:
    """The tracked output-styles tree in the agent checkout -- the source of truth."""
    return repo_path(STYLES_REPO) / "output-styles"


def config_root() -> Path:
    """The user's Claude config dir. Honors CLAUDE_CONFIG_DIR like Claude Code does."""
    return claude_config_root()


def dest_dir() -> Path:
    """Where per-user output styles live."""
    return config_root() / "output-styles"


def available() -> list[Path]:
    """Every shared style file in the source tree."""
    src = source_dir()
    if not src.is_dir():
        return []
    return sorted(
        p for p in src.iterdir() if p.is_file() and p.suffix == ".md" and p.name not in IGNORED
    )


def _require_source() -> Path:
    src = source_dir()
    if not src.is_dir():
        raise AgentConfigError(
            f"no output-styles tree at {src} -- run `agent pull` to fetch the agent repo first"
        )
    return src


def status(name: str) -> str:
    """Link state of one style file in the destination.

    ok        -- symlink resolves to our source
    missing   -- nothing there yet
    stale     -- a symlink, but pointing elsewhere (relinked on install)
    conflict  -- a real file, not a symlink: a hand-written style we won't touch
    """
    link = dest_dir() / name
    src = source_dir() / name
    if link.is_symlink():
        return "ok" if Path(os.path.realpath(link)) == src.resolve() else "stale"
    if link.exists():
        return "conflict"
    return "missing"


def _prune(dest: Path) -> list[str]:
    """Drop links to styles that left the source tree (renamed, or retired).

    Two guards, both from `skills.py`. A link is only pruned if it points INTO
    our source tree -- a style the user wrote, or linked from somewhere else, is
    never our business -- and only if the style is really gone: a dangling link
    to a style still in the tree is a *stale* link, which the install loop
    relinks.
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
    """Symlink every shared style into the user's Claude output-styles dir.

    Idempotent. Never clobbers a real file the user placed there themselves
    (that is a 'conflict' -- reported, left alone). Returns (name, outcome) per
    style.
    """
    _require_source()
    dest = dest_dir()
    dest.mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, str]] = [(name, "pruned") for name in _prune(dest)]
    for style in available():
        name = style.name
        link = dest / name
        state = status(name)
        if state == "ok":
            results.append((name, "ok"))
        elif state == "conflict":
            results.append((name, "SKIP: a non-symlink already exists here"))
        else:  # missing or stale -- (re)create the link
            if link.is_symlink():
                link.unlink()
            link.symlink_to(style)
            results.append((name, "relinked" if state == "stale" else "linked"))
    return results


def check() -> tuple[bool, str]:
    """Doctor probe: are the shared styles installed and pointing at the source?

    Not-yet-installed is a clean, expected state, so it is reported without
    failing -- `agent output-styles install` is the fix, not a repair. Which
    style is *active* is a settings key, so `settings.check()` already reports
    it; this probe stays about the tree.
    """
    src = source_dir()
    if not src.is_dir():
        return False, f"no output-styles tree at {src} -- run `agent pull`"

    styles = available()
    if not styles:
        return True, f"{src}: none defined"

    states = {s.name: status(s.name) for s in styles}
    broken = {n: st for n, st in states.items() if st in ("stale", "conflict")}
    if broken:
        detail = ", ".join(f"{n}: {st}" for n, st in sorted(broken.items()))
        return False, f"{detail} -- run `agent output-styles install`"

    linked = sum(1 for st in states.values() if st == "ok")
    if linked < len(styles):
        return (
            True,
            f"{linked}/{len(styles)} linked -- run `agent output-styles install` for the rest",
        )
    return True, f"{linked} linked into {dest_dir()}"
