"""Freshness: is the checkout current, and would `agent install` change anything.

The git half is exercised against a real pair of repos rather than a mocked
`git`, because the whole value of the check is that it agrees with git -- a fake
that returns "3 commits behind" proves nothing about `rev-list`.
"""

import importlib
import pkgutil
import subprocess

import pytest

import agentcli
from agentcli import freshness, git


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def checkout(monkeypatch, tmp_path):
    """A clone of a local origin, wired in as the agent repo. Returns (clone, origin)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    _git("config", "user.email", "t@example.com", cwd=origin)
    _git("config", "user.name", "t", cwd=origin)
    (origin / "seed").write_text("1\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-qm", "seed", cwd=origin)

    clone = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(clone))
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    monkeypatch.setattr(freshness, "repo", lambda: clone)
    return clone, origin


def _commit_on_origin(origin, text):
    (origin / "seed").write_text(text)
    _git("commit", "-qam", f"change {text}", cwd=origin)


def test_behind_is_zero_on_a_fresh_clone(checkout):
    assert freshness.behind() == 0


def test_behind_counts_commits_after_a_fetch(checkout):
    clone, origin = checkout
    _commit_on_origin(origin, "2")
    _commit_on_origin(origin, "3")

    # Before fetching, the on-disk ref has not moved -- which is exactly the
    # trade the no-fetch default makes.
    assert freshness.behind() == 0
    assert freshness.refresh()
    assert freshness.behind() == 2


def test_behind_is_none_without_a_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(freshness, "repo", lambda: tmp_path / "nothing")
    assert freshness.behind() is None


def test_drift_reports_a_behind_checkout(checkout, monkeypatch):
    clone, origin = checkout
    _commit_on_origin(origin, "2")
    monkeypatch.setattr(freshness, "tree_drift", list)

    found = freshness.drift(fetch=True)
    assert [d.label for d in found] == ["checkout"]
    assert "1 commit behind" in found[0].detail
    assert found[0].fix == "agent pull"


def test_drift_says_nothing_when_current(checkout, monkeypatch):
    monkeypatch.setattr(freshness, "tree_drift", list)
    assert freshness.drift(fetch=True) == []


def test_missing_checkout_is_drift_not_silence(monkeypatch, tmp_path):
    """None from behind() must never be read as zero -- that is the failure this
    check exists to catch."""
    monkeypatch.setattr(freshness, "repo", lambda: tmp_path / "nothing")
    monkeypatch.setattr(freshness, "tree_drift", list)
    found = freshness.drift(fetch=False)
    assert [d.label for d in found] == ["checkout"]
    assert "no usable checkout" in found[0].detail


def test_no_fetch_default_kicks_off_a_background_refresh(checkout, monkeypatch):
    calls = []
    monkeypatch.setattr(freshness, "refresh_detached", lambda: calls.append(1))
    monkeypatch.setattr(freshness, "fetch_age", lambda: 10_000)
    monkeypatch.setattr(freshness, "tree_drift", list)

    freshness.drift(max_age=900)
    assert calls == [1]


def test_a_recent_fetch_skips_the_refresh(checkout, monkeypatch):
    calls = []
    monkeypatch.setattr(freshness, "refresh_detached", lambda: calls.append(1))
    monkeypatch.setattr(freshness, "fetch_age", lambda: 5)
    monkeypatch.setattr(freshness, "tree_drift", list)

    freshness.drift(max_age=900)
    assert calls == []


def test_refresh_survives_an_unreachable_origin(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise git.AgentGitError("no network")

    monkeypatch.setattr(freshness, "repo", lambda: tmp_path)
    monkeypatch.setattr(freshness.git, "run", boom)
    assert freshness.refresh() is False


def test_check_is_offline_tolerant(checkout, monkeypatch):
    monkeypatch.setattr(freshness, "refresh", lambda *a, **k: False)
    monkeypatch.setattr(freshness, "tree_drift", list)
    ok, detail = freshness.check()
    assert ok
    assert "offline" in detail


def test_summary_leads_with_the_fix():
    found = [
        freshness.Drift("checkout", "2 commits behind origin", "agent pull"),
        freshness.Drift("skills", "new-skill", "agent skills install"),
    ]
    message = freshness.summary(found)
    assert "agent pull && agent skills install" in message
    assert "2 commits behind" in message


def test_summary_is_empty_when_current():
    assert freshness.summary([]) == ""


def test_tree_drift_reports_an_unlinked_member(monkeypatch, tmp_path):
    from agentcli import skills

    src = tmp_path / "skills"
    (src / "newone").mkdir(parents=True)
    (src / "newone" / "SKILL.md").write_text("x\n")
    monkeypatch.setattr(skills, "source_dir", lambda: src)
    monkeypatch.setattr(skills, "dest_dir", lambda: tmp_path / "dest")

    found = {d.label: d for d in freshness.tree_drift()}
    assert "newone" in found["skills"].detail
    assert found["skills"].fix == "agent skills install"


# --- sync_here: the explore half -------------------------------------------
#
# `agent workspace create` already fetches and fast-forwards before it cuts a
# worktree, so implementation starts fresh. These cover the case that had
# nothing: a session that opens a checkout and starts reading.


@pytest.fixture
def managed(monkeypatch, tmp_path, checkout):
    """The clone from `checkout`, placed where src_root() will find it."""
    clone, origin = checkout
    root = tmp_path / "src"
    root.mkdir()
    managed_path = root / "demo"
    clone.rename(managed_path)
    monkeypatch.setenv("AGENT_SRC_ROOT", str(root))
    monkeypatch.setattr(freshness, "repo", lambda: managed_path)
    return managed_path, origin


def test_sync_here_fast_forwards_a_behind_checkout(managed):
    path, origin = managed
    _commit_on_origin(origin, "2")

    message = freshness.sync_here(path)
    assert message == "demo: pulled 1 commit from origin/main"
    assert (path / "seed").read_text() == "2"


def test_sync_here_is_silent_when_already_current(managed):
    path, _ = managed
    assert freshness.sync_here(path) == ""


def test_sync_here_finds_the_checkout_from_a_subdirectory(managed):
    path, origin = managed
    _commit_on_origin(origin, "2")
    nested = path / "deep" / "deeper"
    nested.mkdir(parents=True)

    assert "pulled 1 commit" in freshness.sync_here(nested)


def test_sync_here_declines_on_a_feature_branch(managed):
    path, origin = managed
    _commit_on_origin(origin, "2")
    _git("checkout", "-qb", "feat/mine", cwd=path)

    assert freshness.sync_here(path) == ""
    assert (path / "seed").read_text() == "1\n"  # still the pre-fetch content


def test_sync_here_declines_on_a_dirty_tree(managed):
    path, origin = managed
    _commit_on_origin(origin, "2")
    (path / "seed").write_text("local edit\n")

    message = freshness.sync_here(path)
    assert "uncommitted changes" in message
    assert (path / "seed").read_text() == "local edit\n"


def test_sync_here_reports_a_diverged_branch_without_touching_it(managed):
    path, origin = managed
    _commit_on_origin(origin, "2")
    (path / "local").write_text("mine\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "local work", cwd=path)

    message = freshness.sync_here(path)
    assert "not fast-forwardable" in message
    assert (path / "local").is_file()


def test_sync_here_ignores_a_path_outside_the_src_root(managed, tmp_path):
    assert freshness.sync_here(tmp_path / "elsewhere") == ""


def test_checkout_for_excludes_worktrees(managed, tmp_path):
    """Worktrees live outside the src root, so they can never be a candidate --
    which is the guard that keeps a feature branch with work on it untouched."""
    path, _ = managed
    worktree = tmp_path / "worktrees" / "demo" / "session-x"
    worktree.parent.mkdir(parents=True)
    _git("worktree", "add", "-q", "-b", "feat/x", str(worktree), cwd=path)

    assert freshness.checkout_for(worktree) is None


# The point of this one: a new distribution tree that nobody adds to TREES would
# be silently unmonitored, and the check that exists to catch drift would itself
# have drifted. Discovery beats a hand-kept list.
def test_every_distribution_tree_is_monitored():
    discovered = set()
    for info in pkgutil.iter_modules(agentcli.__path__):
        try:
            module = importlib.import_module(f"agentcli.{info.name}")
        except ImportError:  # pragma: no cover -- an optional dep, not a tree
            continue
        if hasattr(module, "source_dir") and hasattr(module, "check"):
            discovered.add(info.name)

    monitored = {module.__name__.rsplit(".", 1)[-1] for _label, module, _p, _f in freshness.TREES}
    assert discovered == monitored, (
        f"distribution trees not in freshness.TREES: {sorted(discovered - monitored)}"
    )
