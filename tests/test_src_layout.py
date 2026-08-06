"""The owner-scoped checkout layout.

One env var changes where every checkout is looked for, so the risk is not that
owner-scoped resolution is wrong -- it is that turning it on somewhere quietly
changes behaviour on the workstation, or that some module keeps its own idea of
how deep a checkout sits. Both halves are pinned here.
"""

from __future__ import annotations

import pytest

from agentcli import freshness, git, pull, repos, workspace
from agentcli.config import repo_path, src_layout, worktree_base
from agentcli.errors import AgentConfigError


@pytest.fixture
def root(tmp_path, monkeypatch):
    resolved = tmp_path.resolve()
    monkeypatch.setenv("AGENT_SRC_ROOT", str(resolved))
    return resolved


@pytest.fixture
def owner_root(root, monkeypatch):
    monkeypatch.setenv("AGENT_SRC_LAYOUT", "owner")
    return root


def checkout(root, *parts):
    path = root.joinpath(*parts)
    (path / ".git").mkdir(parents=True)
    return path


# --- the default is unchanged ---------------------------------------------


def test_the_layout_defaults_to_flat(root):
    """The workstation must not need to opt out of a container-only change."""
    assert src_layout() == "flat"
    assert repo_path("agent") == root / "agent"


def test_flat_ignores_an_owner_in_the_name(root):
    """`agent pull` passes a full slug in both layouts; flat means one directory."""
    assert repo_path("brujoand/agent") == root / "agent"


@pytest.mark.parametrize("layout", ["OWNER", " owner "])
def test_the_layout_value_is_forgiving(root, monkeypatch, layout):
    monkeypatch.setenv("AGENT_SRC_LAYOUT", layout)
    checkout(root, "brujoand", "agent")
    assert repo_path("agent") == root / "brujoand" / "agent"


def test_an_unknown_layout_is_flat_not_an_error(root, monkeypatch):
    # Fail toward the layout that is safe to be wrong about: flat resolves to a
    # path that either exists or visibly does not.
    monkeypatch.setenv("AGENT_SRC_LAYOUT", "nested")
    assert repo_path("agent") == root / "agent"


# --- resolving a repo -------------------------------------------------------


def test_a_bare_name_finds_its_owner(owner_root):
    checkout(owner_root, "brujoand", "agent")
    assert repo_path("agent") == owner_root / "brujoand" / "agent"


def test_a_qualified_name_needs_no_scan(owner_root):
    """Naming the owner is exact -- it resolves before the repo is even cloned."""
    assert repo_path("brujoand/agent") == owner_root / "brujoand" / "agent"


def test_a_colliding_bare_name_is_refused(owner_root):
    """Never resolved by directory order: the same name must mean one repo."""
    checkout(owner_root, "brujoand", "agent")
    checkout(owner_root, "anthropics", "agent")

    with pytest.raises(AgentConfigError) as excinfo:
        repo_path("agent")

    message = str(excinfo.value)
    assert "anthropics/agent" in message and "brujoand/agent" in message
    # The error has to carry the way out of it, not just the diagnosis.
    assert "<owner>/agent" in message


def test_a_collision_is_resolved_by_qualifying(owner_root):
    checkout(owner_root, "brujoand", "agent")
    checkout(owner_root, "anthropics", "agent")

    assert repo_path("anthropics/agent") == owner_root / "anthropics" / "agent"


def test_an_unknown_repo_yields_an_absent_path_rather_than_raising(owner_root):
    """Callers report "run `agent pull` first"; raising would make that a traceback."""
    checkout(owner_root, "brujoand", "agent")

    path = repo_path("nope")

    assert not path.exists()
    assert not git.is_checkout(path)


def test_an_owner_sharing_a_repo_name_is_not_a_false_positive(owner_root):
    """`<root>/<repo>/<repo>` is the miss path; an owner directory must not satisfy it."""
    checkout(owner_root, "brujoand", "dotfiles")

    assert not repo_path("brujoand").is_dir()


def test_a_file_beside_the_owners_is_skipped(owner_root):
    (owner_root / "README").write_text("shared mount, human writes here too\n")
    checkout(owner_root, "brujoand", "agent")

    assert repo_path("agent") == owner_root / "brujoand" / "agent"


# --- worktrees --------------------------------------------------------------


def test_worktrees_mirror_the_checkout_shape(owner_root, monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    checkout(owner_root, "brujoand", "agent")

    assert worktree_base("agent") == home / "worktrees" / "brujoand" / "agent"


def test_both_names_of_a_repo_share_one_worktree_base(owner_root):
    """Otherwise `workspace create` and `workspace gc` could disagree on location."""
    checkout(owner_root, "brujoand", "agent")

    assert worktree_base("agent") == worktree_base("brujoand/agent")


def test_flat_worktrees_are_unchanged(root, monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    assert worktree_base("agent") == home / "worktrees" / "agent"


# --- enumeration ------------------------------------------------------------


def test_managed_repos_are_qualified_in_owner_mode(owner_root):
    """Bare names would start failing the day two owners collide."""
    checkout(owner_root, "brujoand", "agent")
    checkout(owner_root, "brujoand", "dotfiles")
    checkout(owner_root, "anthropics", "agent")

    assert workspace.managed_repos() == [
        "anthropics/agent",
        "brujoand/agent",
        "brujoand/dotfiles",
    ]


def test_every_enumerated_repo_round_trips_through_repo_path(owner_root):
    """The contract the callers rely on: enumerate, then resolve, with no collisions."""
    checkout(owner_root, "brujoand", "agent")
    checkout(owner_root, "anthropics", "agent")

    for name in workspace.managed_repos():
        assert git.is_checkout(repo_path(name))


def test_enumeration_ignores_a_non_checkout_directory(owner_root):
    checkout(owner_root, "brujoand", "agent")
    (owner_root / "brujoand" / "notes").mkdir()

    assert workspace.managed_repos() == ["brujoand/agent"]


def test_flat_enumeration_stays_bare(root):
    checkout(root, "agent")
    checkout(root, "dotfiles")

    assert workspace.managed_repos() == ["agent", "dotfiles"]


# --- the modules that measure depth themselves ------------------------------


def test_freshness_finds_the_checkout_two_levels_down(owner_root):
    repo = checkout(owner_root, "brujoand", "agent")

    assert freshness.checkout_for(repo / "agentcli" / "config.py") == repo


def test_freshness_does_not_mistake_an_owner_for_a_checkout(owner_root):
    """One level down is an owner directory in this layout, never a repo."""
    checkout(owner_root, "brujoand", "agent")

    assert freshness.checkout_for(owner_root / "brujoand") is None


def test_freshness_is_unchanged_when_flat(root):
    repo = checkout(root, "agent")

    assert freshness.checkout_for(repo / "agentcli") == repo


# --- pull -------------------------------------------------------------------


def test_pull_clones_into_the_owner_directory(owner_root, monkeypatch, capsys):
    monkeypatch.setattr(repos, "clone_urls", lambda: ["https://github.com/brujoand/agent.git"])
    cloned: list[tuple[str, str]] = []
    monkeypatch.setattr(git, "clone", lambda url, dest: cloned.append((url, str(dest))))
    monkeypatch.setattr(git, "set_github_helper", lambda repo, worktree=False: None)

    assert pull.run() == 0

    assert cloned == [
        ("https://github.com/brujoand/agent.git", str(owner_root / "brujoand" / "agent"))
    ]
    # The line printed is the shape on disk, so what it says and where the repo
    # landed cannot drift apart.
    assert "cloned brujoand/agent" in capsys.readouterr().out


def test_pull_keeps_owners_apart(owner_root, monkeypatch):
    """Two repos of the same name are two checkouts here, not one overwritten twice."""
    monkeypatch.setattr(
        repos,
        "clone_urls",
        lambda: [
            "https://github.com/brujoand/agent.git",
            "https://github.com/anthropics/agent.git",
        ],
    )
    cloned: list[str] = []
    monkeypatch.setattr(git, "clone", lambda url, dest: cloned.append(str(dest)))
    monkeypatch.setattr(git, "set_github_helper", lambda repo, worktree=False: None)

    assert pull.run() == 0

    assert cloned == [
        str(owner_root / "brujoand" / "agent"),
        str(owner_root / "anthropics" / "agent"),
    ]


def test_pull_stays_flat_by_default(root, monkeypatch, capsys):
    monkeypatch.setattr(repos, "clone_urls", lambda: ["https://github.com/brujoand/agent.git"])
    cloned: list[str] = []
    monkeypatch.setattr(git, "clone", lambda url, dest: cloned.append(str(dest)))
    monkeypatch.setattr(git, "set_github_helper", lambda repo, worktree=False: None)

    assert pull.run() == 0

    assert cloned == [str(root / "agent")]
    assert "cloned agent" in capsys.readouterr().out
