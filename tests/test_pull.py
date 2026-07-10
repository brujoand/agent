from __future__ import annotations

from pathlib import Path

import pytest

from agentcli import git, pull, repos


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    monkeypatch.setattr(pull, "agent_root", lambda: tmp_path)
    monkeypatch.setattr(git, "agent_root", lambda: tmp_path)
    return tmp_path


def test_self_repo_is_skipped_but_its_helper_is_still_asserted(fake_root, monkeypatch):
    """`agent pull` never clones itself -- and must still keep its own helper current.

    Regression: the agent root was the one checkout nothing repointed. `pull`
    skips it (cloning would nest agent/ inside agent/) and `doctor` only walked
    subdirectories, so it kept a stale helper and its own `git push` broke.
    """
    monkeypatch.setattr(repos, "clone_urls", lambda: ["https://github.com/brujoand/agent.git"])
    monkeypatch.setattr(pull, "_self_slug", lambda root: "brujoand/agent")

    asserted: list[Path] = []
    monkeypatch.setattr(
        git, "set_github_helper", lambda repo, worktree=False: asserted.append(repo)
    )

    assert pull.run() == 0
    assert asserted == [fake_root]


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


def test_helper_spec_is_absolute_path_to_launcher(fake_root):
    # git runs a `!`-prefixed helper as a shell command, from inside an arbitrary
    # repo with a minimal environment. A bare `agent` would depend on PATH.
    spec = git.helper_spec()
    assert spec == f"!{fake_root / 'agent'} git-credential"
    assert spec.startswith("!/")
