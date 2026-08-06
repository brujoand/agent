"""The host-side launcher: what it finds, what it accepts, what it mounts.

The two things worth testing are pure. Repo resolution decides what a human's
keystrokes mean, and `docker_command` decides what the agent can reach --
neither needs Docker to be exercised, and the mount set in particular is a
security property, not a formatting detail.
"""

from __future__ import annotations

import time

import pytest
import typer

from agentcli import bagent


def _checkout(root, owner, name):
    path = root / owner / name
    (path / ".git").mkdir(parents=True)
    return path


@pytest.fixture
def src(tmp_path, monkeypatch):
    root = tmp_path / "src"
    root.mkdir()
    monkeypatch.setenv("BAGENT_SRC", str(root))
    monkeypatch.setenv("BAGENT_HOME", str(tmp_path / "bagent"))
    return root


# --- discovery ---------------------------------------------------------------


def test_discovers_owner_scoped_checkouts(src):
    _checkout(src, "brujoand", "agent")
    _checkout(src, "brujoand", "dotfiles")
    _checkout(src, "someone", "infra")

    found = bagent.discover(src)

    assert [r.slug for r in found] == ["brujoand/agent", "brujoand/dotfiles", "someone/infra"]


def test_a_directory_without_git_is_not_a_repo(src):
    (src / "brujoand" / "notes").mkdir(parents=True)
    _checkout(src, "brujoand", "agent")

    # /mnt/src is a shared mount; the human may keep anything there.
    assert [r.name for r in bagent.discover(src)] == ["agent"]


def test_a_missing_root_is_empty_not_an_error(tmp_path):
    assert bagent.discover(tmp_path / "nope") == []


def test_worktrees_as_git_files_still_count(src):
    # A worktree's `.git` is a FILE, not a directory. `.exists()` covers both.
    path = src / "brujoand" / "wt"
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: /elsewhere\n")

    assert [r.name for r in bagent.discover(src)] == ["wt"]


# --- what a human may type ---------------------------------------------------


def test_unique_names_complete_bare(src):
    _checkout(src, "brujoand", "agent")
    _checkout(src, "brujoand", "dotfiles")

    assert bagent.tokens(bagent.discover(src)) == ["agent", "dotfiles"]


def test_colliding_names_force_the_owner_on_both(src):
    _checkout(src, "brujoand", "infra")
    _checkout(src, "someone", "infra")
    _checkout(src, "brujoand", "agent")

    # BOTH become owner-qualified. If one kept the short name, which repo
    # `bagent infra` meant would depend on directory order.
    assert bagent.tokens(bagent.discover(src)) == ["agent", "brujoand/infra", "someone/infra"]


def test_resolves_a_bare_unique_name(src):
    _checkout(src, "brujoand", "agent")
    assert bagent.resolve("agent", bagent.discover(src)).slug == "brujoand/agent"


def test_resolves_an_owner_qualified_name(src):
    _checkout(src, "brujoand", "infra")
    _checkout(src, "someone", "infra")

    assert bagent.resolve("someone/infra", bagent.discover(src)).owner == "someone"


def test_an_ambiguous_name_is_refused_and_names_the_candidates(src):
    _checkout(src, "brujoand", "infra")
    _checkout(src, "someone", "infra")

    with pytest.raises(typer.BadParameter) as err:
        bagent.resolve("infra", bagent.discover(src))

    assert "ambiguous" in str(err.value)
    assert "brujoand/infra" in str(err.value)
    assert "someone/infra" in str(err.value)


def test_an_unknown_name_says_where_it_looked(src):
    _checkout(src, "brujoand", "agent")

    with pytest.raises(typer.BadParameter) as err:
        bagent.resolve("nope", bagent.discover(src))
    assert "--list" in str(err.value)

    with pytest.raises(typer.BadParameter) as err:
        bagent.resolve("brujoand/nope", bagent.discover(src))
    assert str(src) in str(err.value)


def test_completion_filters_on_the_prefix(src, monkeypatch):
    _checkout(src, "brujoand", "agent")
    _checkout(src, "brujoand", "dotfiles")

    assert bagent.complete_repo("d") == ["dotfiles"]
    assert bagent.complete_repo("") == ["agent", "dotfiles"]


def test_typing_the_repo_name_reaches_a_collision(src):
    _checkout(src, "brujoand", "infra")
    _checkout(src, "someone", "infra")

    # The name is what a human knows and types. Matching only the whole token
    # would offer nothing here, since both candidates start with their owner.
    assert bagent.complete_repo("in") == ["brujoand/infra", "someone/infra"]
    # Typing the owner still narrows it the other way.
    assert bagent.complete_repo("someone/") == ["someone/infra"]


def test_completion_never_raises(monkeypatch):
    # A completer that throws breaks the user's shell, not just this command.
    monkeypatch.setattr(bagent, "discover", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert bagent.complete_repo("x") == []


# --- what the container can reach -------------------------------------------


def test_only_two_paths_are_mounted_by_default(src, tmp_path):
    _checkout(src, "brujoand", "agent")
    repo = bagent.resolve("agent", bagent.discover(src))

    argv = bagent.docker_command(repo, command=["claude"])

    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--volume"]
    assert len(mounts) == 2, f"default mounts changed: {mounts}"
    assert any(str(src) in m for m in mounts)
    assert any(str(tmp_path / "bagent") in m for m in mounts)


def test_home_is_the_mount_so_state_persists(src, tmp_path):
    _checkout(src, "brujoand", "agent")
    argv = bagent.docker_command(bagent.resolve("agent", bagent.discover(src)))

    assert f"HOME={tmp_path / 'bagent'}" in argv
    # And the agent's own CLI is pointed at the shared checkouts.
    assert f"AGENT_SRC_ROOT={src}" in argv


def test_workdir_is_the_resolved_repo(src):
    _checkout(src, "brujoand", "agent")
    repo = bagent.resolve("agent", bagent.discover(src))

    argv = bagent.docker_command(repo)

    assert argv[argv.index("--workdir") + 1] == str(repo.path)


def test_the_command_lands_after_the_image(src):
    _checkout(src, "brujoand", "agent")
    repo = bagent.resolve("agent", bagent.discover(src))

    argv = bagent.docker_command(repo, command=["bash", "-lc", "ls"], img="img:1")

    assert argv[-4:] == ["img:1", "bash", "-lc", "ls"]


def test_it_is_interactive_and_disposable(src):
    _checkout(src, "brujoand", "agent")
    argv = bagent.docker_command(bagent.resolve("agent", bagent.discover(src)))

    for flag in ("--rm", "--interactive", "--tty"):
        assert flag in argv


# --- elevated access ---------------------------------------------------------


@pytest.mark.parametrize("name", sorted(bagent.PROFILES))
def test_every_profile_mounts_read_only_under_home(src, tmp_path, name):
    _checkout(src, "brujoand", "agent")
    repo = bagent.resolve("agent", bagent.discover(src))

    argv = bagent.docker_command(repo, profiles=[name])

    lent = [argv[i + 1] for i, a in enumerate(argv) if a == "--volume"][2:]
    assert len(lent) == 1
    assert lent[0].endswith(":ro"), "a lent credential must never be writable"
    assert str(tmp_path / "bagent") in lent[0]


def test_no_credential_is_lent_unless_asked(src):
    _checkout(src, "brujoand", "agent")
    argv = bagent.docker_command(bagent.resolve("agent", bagent.discover(src)))

    joined = " ".join(argv)
    assert ".kube" not in joined
    assert ".talos" not in joined
    assert ".ssh" not in joined


def test_the_ssh_profile_lends_one_key_not_the_whole_directory():
    # Mounting ~/.ssh would hand over every key and known_hosts with it, and
    # would land on top of the agent's own ssh dir.
    profile = bagent.PROFILES["ssh"]
    assert profile.source.endswith("id_ed25519")
    assert not profile.target.startswith(".ssh/")


def test_an_unknown_profile_is_refused_with_the_known_ones(src):
    _checkout(src, "brujoand", "agent")
    repo = bagent.resolve("agent", bagent.discover(src))

    with pytest.raises(typer.BadParameter) as err:
        bagent.docker_command(repo, profiles=["cluster-admin"])

    assert "cluster-admin" in str(err.value)
    assert "kube" in str(err.value)


def test_ad_hoc_mounts_pass_through(src):
    _checkout(src, "brujoand", "agent")
    repo = bagent.resolve("agent", bagent.discover(src))

    argv = bagent.docker_command(repo, mounts=["/opt/data:/opt/data:ro"])

    assert "/opt/data:/opt/data:ro" in argv


def test_user_override_is_passed_only_when_given(src):
    _checkout(src, "brujoand", "agent")
    repo = bagent.resolve("agent", bagent.discover(src))

    assert "--user" not in bagent.docker_command(repo)

    argv = bagent.docker_command(repo, user="1000:1000")
    assert argv[argv.index("--user") + 1] == "1000:1000"


# --- keeping the image current ----------------------------------------------


def test_pulls_when_never_pulled(tmp_path):
    assert bagent.should_pull(tmp_path / "never", time.time()) is True


def test_does_not_pull_within_the_ttl(tmp_path):
    stamp = tmp_path / "stamp"
    stamp.touch()
    assert bagent.should_pull(stamp, time.time()) is False


def test_pulls_once_the_ttl_has_passed(tmp_path):
    stamp = tmp_path / "stamp"
    stamp.touch()
    assert bagent.should_pull(stamp, time.time() + bagent.PULL_TTL_SECONDS + 1) is True


def test_a_failed_pull_still_records_the_attempt(tmp_path, monkeypatch):
    # Offline must not block a launch, and must not retry on every command.
    stamp = tmp_path / "cache" / "stamp"

    class Failed:
        returncode = 1

    monkeypatch.setattr(bagent.subprocess, "run", lambda *a, **k: Failed())
    bagent.pull_image("img:1", stamp)

    assert stamp.exists()
