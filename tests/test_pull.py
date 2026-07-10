from __future__ import annotations

from pathlib import Path

import pytest

from agentcli import git, pull, repos


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    monkeypatch.setattr(pull, "src_root", lambda: tmp_path)
    monkeypatch.setattr(git, "INSTALLED_AGENT", tmp_path / ".local" / "bin" / "agent")
    return tmp_path


def test_agent_repo_is_an_ordinary_sibling(fake_root, monkeypatch):
    """No self-skip: with checkouts as siblings, the agent repo is just another one.

    Regression guard for the old nested layout, where `pull` skipped its own repo
    and `doctor` only walked subdirectories -- so the agent checkout kept a stale
    credential helper and its own `git push` broke.
    """
    monkeypatch.setattr(repos, "clone_urls", lambda: ["https://github.com/brujoand/agent.git"])
    dest = fake_root / "agent"
    (dest / ".git").mkdir(parents=True)

    asserted: list[Path] = []
    monkeypatch.setattr(
        git, "set_github_helper", lambda repo, worktree=False: asserted.append(repo)
    )
    monkeypatch.setattr(git, "fast_forward", lambda repo: None)

    assert pull.run() == 0
    assert asserted == [dest]


def test_sync_one_reasserts_helper_on_existing_checkout(fake_root, monkeypatch):
    dest = fake_root / "repo"
    (dest / ".git").mkdir(parents=True)

    asserted: list[Path] = []
    monkeypatch.setattr(
        git, "set_github_helper", lambda repo, worktree=False: asserted.append(repo)
    )
    monkeypatch.setattr(git, "fast_forward", lambda repo: None)

    assert pull.sync_one("https://github.com/brujoand/repo.git", dest) == "updated"
    assert asserted == [dest]


def test_sync_one_clones_when_absent(fake_root, monkeypatch):
    cloned: list[tuple[str, Path]] = []
    monkeypatch.setattr(git, "clone", lambda url, dest: cloned.append((url, dest)))
    dest = fake_root / "new"
    assert pull.sync_one("https://github.com/brujoand/new.git", dest) == "cloned"
    assert cloned == [("https://github.com/brujoand/new.git", dest)]


def test_helper_spec_points_at_the_installed_agent_not_the_checkout(fake_root):
    """The helper must survive `git checkout` of a commit predating the CLI.

    If it pointed into the checkout, switching to such a commit would delete both
    the launcher and agentcli/, leaving git with no way to authenticate (there is
    no ambient credential store) -- so `git pull` could not fetch the commits that
    would restore it.
    """
    spec = git.helper_spec()
    assert spec == f"!{fake_root / '.local' / 'bin' / 'agent'} git-credential"
    # Absolute: git spawns it from inside an arbitrary repo with a minimal env,
    # so a bare `agent` would depend on PATH.
    assert spec.startswith("!/")
    # And it must not name the checkout's own launcher.
    assert not spec.startswith(f"!{fake_root / 'agent'} ")
