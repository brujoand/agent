"""Is this session running the rules, skills and hooks that are on origin?

`agent doctor` already answers half the question: it reports, per distribution
tree, whether the links and imports exist and point at the checkout. What nothing
answered until now is the other half -- whether the *checkout* is current. The
symlink design makes that gap silent: a `~/src/agent` twelve commits behind
resolves every link cleanly, so every probe passes while the rules being loaded
are the old ones.

So freshness is two questions, and both have to be asked:

1. **Is the checkout behind `origin/<default>`?** `agent pull` is the fix, and
   because the links point INTO the checkout, that one command re-points every
   session on the host at the new content -- no reinstall.
2. **Would `agent install` change anything?** Pulling updates the *contents* of
   what is already linked. It cannot link a skill, rule or style that did not
   exist when install last ran. A newly added tree member is invisible until
   install runs again, which is exactly the case a human never thinks to check.

Both are cheap and neither is guesswork, which is the point: this is a check that
belongs in a command and a hook, not in a reasoning turn.

## Why the default does not fetch

This runs on a session's critical path (SessionStart), so it must not depend on
the network being up or fast. `drift()` compares against the `origin/<default>`
ref already on disk -- no network, sub-millisecond -- and, when that ref has gone
stale, spawns a detached `git fetch` so the *next* session compares against fresh
data. The cost of that trade is bounded and worth naming: a push to origin can go
unnoticed for one session. `check()` (what `agent doctor` calls) fetches
synchronously instead, because doctor is the command you run when you want the
real answer and are willing to wait for it.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from agentcli import git, hooks, rules, settings, skills
from agentcli.config import repo_path, src_root
from agentcli.errors import AgentError

# The repo that owns every distribution tree, so it is the only one whose
# freshness decides what a session loads.
FRESHNESS_REPO = "agent"

# How old the on-disk `origin/<default>` ref may be before a background refresh
# is kicked off. Fifteen minutes: long enough that a burst of sessions does not
# spawn a fetch each, short enough that a morning's work is not judged against
# yesterday's remote.
DEFAULT_MAX_AGE = 900

# A network fetch on a session's critical path gets a hard bound. Whatever has
# not answered in this long is not going to help, and a hung hook is worse than a
# missed warning.
FETCH_TIMEOUT = 8.0


@dataclass(frozen=True)
class Drift:
    """One thing that is not current, and the command that fixes it."""

    label: str
    detail: str
    fix: str

    def line(self) -> str:
        return f"{self.label}: {self.detail} -- run `{self.fix}`"


def repo() -> Path:
    return repo_path(FRESHNESS_REPO)


def _per_item(module) -> list[str]:
    """Members of a symlink tree that install would (re)link. Empty means current."""
    return [item.name for item in module.available() if module.status(item.name) != "ok"]


def _skills_drift() -> list[str]:
    return _per_item(skills)


def _rules_drift() -> list[str]:
    state = rules.status()
    return [] if state == "ok" else [f"memory block {state}"]


def _hooks_drift() -> list[str]:
    # Two halves, so two ways to be stale: the scripts, and the settings wiring
    # that decides whether they ever fire.
    found = _per_item(hooks)
    wiring = hooks.settings_status()
    if wiring != "ok":
        found.append(f"wiring {wiring}")
    return found


def _settings_drift() -> list[str]:
    state = settings.status()
    return [] if state == "ok" else [f"declared keys {state}"]


# One entry per distribution tree: (label, module, probe, fix). Adding a tree
# means adding a line here -- and `tests/test_freshness.py` discovers the
# distribution modules and fails if one is missing, so the drift checker cannot
# itself drift. The module is carried for exactly that test; the label is what
# the reader sees, and the two need not match.
TREES = (
    ("skills", skills, _skills_drift, "agent skills install"),
    ("rules", rules, _rules_drift, "agent rules install"),
    ("hooks", hooks, _hooks_drift, "agent hooks install"),
    ("settings", settings, _settings_drift, "agent settings install"),
)


def fetch_age() -> float | None:
    """Seconds since the last fetch, or None if this checkout has never fetched."""
    marker = repo() / ".git" / "FETCH_HEAD"
    try:
        return time.time() - marker.stat().st_mtime
    except OSError:
        return None


def refresh_detached() -> None:
    """Update the remote refs in the background. Never blocks, never reports.

    Deliberately fire-and-forget: its whole purpose is to make the *next* run
    accurate, so there is nothing for this run to wait for. Failure is silent
    because a machine that is offline is not a machine with a problem to report.
    """
    with contextlib.suppress(OSError):
        subprocess.Popen(  # noqa: S603
            ["git", "-C", str(repo()), "fetch", "--quiet", "origin"],  # noqa: S607
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )


def refresh(timeout: float = FETCH_TIMEOUT) -> bool:
    """Fetch now, bounded. True if the remote refs are current as of this moment."""
    try:
        git.run(["-C", str(repo()), "fetch", "--quiet", "origin"], timeout=timeout)
        return True
    except AgentError:
        return False


def behind(path: Path | None = None) -> int | None:
    """Commits on `origin/<default>` that the checkout does not have.

    Defaults to the agent repo, since that is the one whose freshness decides
    what a session loads. None when the question cannot be asked at all -- no
    checkout, or no remote ref yet -- which is a different answer from zero and
    must not be reported as "current".
    """
    path = path or repo()
    if not git.is_checkout(path):
        return None
    default = git.default_branch(path)
    result = git.run(
        ["-C", str(path), "rev-list", "--count", f"HEAD..origin/{default}"], check=False
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def checkout_for(path: Path) -> Path | None:
    """The managed primary checkout `path` sits in, or None.

    Only the flat `~/src/<repo>` layout counts. Worktrees live under
    `~/worktrees/`, so they fall outside `src_root()` and are excluded by
    construction -- which is the behaviour wanted: a worktree is on a feature
    branch with work on it, and nothing here may move that.
    """
    root = src_root()
    try:
        relative = Path(path).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    if not relative.parts:
        return None
    candidate = root / relative.parts[0]
    return candidate if git.is_checkout(candidate) else None


def sync_checkout(path: Path, timeout: float = FETCH_TIMEOUT) -> str:
    """Fast-forward a primary checkout to origin. Returns a line to show, or "".

    This is the *explore* half of staying current: `agent workspace create`
    already fetches and rebases the default branch before it cuts a worktree, so
    implementation always starts fresh. Reading does not -- a session that opens
    `~/src/<repo>` and starts grepping works against whatever was last on disk.

    Three guards, and they are the reason this is safe to run unattended: only a
    clean tree, only on the default branch, and only `--ff-only`. Any local work,
    any feature branch, any divergence, and it declines and says so rather than
    touching anything. Agents cannot write to these checkouts anyway (a PreToolUse
    hook blocks it), so in practice the clean-tree guard holds trivially -- it is
    there for the human sharing the machine.
    """
    if not git.is_checkout(path):
        return ""
    name = path.name
    try:
        default = git.default_branch(path)
        if git.current_branch(path) != default:
            return ""
        if git.is_dirty(path):
            return f"{name}: has uncommitted changes, not pulling"
        git.run(["-C", str(path), "fetch", "--quiet", "origin"], timeout=timeout)
    except AgentError:
        # Offline, or a repo that cannot answer. Nothing to say: a machine
        # without a network is not a machine with a problem to report.
        return ""

    count = behind(path)
    if not count:
        return ""
    result = git.run(
        ["-C", str(path), "merge", "--ff-only", "--quiet", f"origin/{default}"], check=False
    )
    plural = "commit" if count == 1 else "commits"
    if result.returncode != 0:
        return f"{name}: {count} {plural} behind origin/{default}, not fast-forwardable"
    return f"{name}: pulled {count} {plural} from origin/{default}"


def sync_here(path: Path | None = None) -> str:
    """Bring the checkout containing `path` (default: cwd) up to date."""
    checkout = checkout_for(path or Path.cwd())
    return sync_checkout(checkout) if checkout else ""


def tree_drift() -> list[Drift]:
    """Every tree that `agent install` would change. Empty means all converged."""
    found: list[Drift] = []
    for label, _module, probe, fix in TREES:
        try:
            items = probe()
        except AgentError as err:
            found.append(Drift(label, str(err).splitlines()[0], fix))
            continue
        if items:
            found.append(Drift(label, ", ".join(items), fix))
    return found


def drift(max_age: float = DEFAULT_MAX_AGE, fetch: bool = False) -> list[Drift]:
    """Everything not current, cheapest first. Empty list means this session is.

    With `fetch=False` (the default, and what the hook uses) the comparison is
    against the refs already on disk; a stale ref triggers a background refresh
    for next time rather than a wait now.
    """
    if fetch:
        refresh()
    else:
        age = fetch_age()
        if age is None or age > max_age:
            refresh_detached()

    found: list[Drift] = []
    count = behind()
    if count is None:
        # Name the root that was searched, not just the path: the launcher in a
        # checkout exports AGENT_SRC_ROOT from its own location, so running
        # `./agent` from a worktree looks for the tree in the wrong place, and
        # the resolved root is what makes that obvious in one read.
        found.append(
            Drift("checkout", f"no usable checkout at {repo()} (root {src_root()})", "agent pull")
        )
    elif count:
        plural = "commit" if count == 1 else "commits"
        found.append(Drift("checkout", f"{count} {plural} behind origin", "agent pull"))
    found.extend(tree_drift())
    return found


def check() -> tuple[bool, str]:
    """Doctor probe. Fetches, because doctor is the command you run to be sure.

    An offline host is reported, not failed: not knowing whether origin has moved
    is a fact about the network, and failing on it would train the reader to
    ignore the row.
    """
    online = refresh()
    found = drift(fetch=False)
    note = "" if online else " (offline: compared against the last fetch)"
    if found:
        return False, "; ".join(d.line() for d in found) + note
    age = fetch_age()
    when = f", fetched {int(age // 60)}m ago" if age is not None else ""
    return True, f"current with origin{when}{note}"


def summary(found: list[Drift]) -> str:
    """The one-line form the SessionStart hook shows the human.

    One line, leading with the fix, because a startup warning competes with the
    thing the reader actually sat down to do.
    """
    if not found:
        return ""
    fixes = []
    for item in found:
        if item.fix not in fixes:
            fixes.append(item.fix)
    # `label: detail` here, not the full `line()`: the fixes are listed once at
    # the end, and repeating each one inline doubles a warning that is already
    # competing with whatever the reader sat down to do.
    detail = "; ".join(f"{d.label}: {d.detail}" for d in found)
    return f"Claude config is not current -- {detail}. Fix: {' && '.join(fixes)}"


def env_max_age() -> float:
    """`AGENT_FRESHNESS_MAX_AGE` overrides the refresh interval, for testing."""
    raw = os.environ.get("AGENT_FRESHNESS_MAX_AGE")
    try:
        return float(raw) if raw else DEFAULT_MAX_AGE
    except ValueError:
        return DEFAULT_MAX_AGE
