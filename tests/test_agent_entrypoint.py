"""The interactive image's entrypoint, exercised as the container runs it.

Small script, but it is the only thing standing between a persisted git
credential helper and an agent that cannot authenticate at all: every clone
records an absolute path to `$HOME/.local/bin/agent`, and inside the container
that path only exists because this script makes it.

Tested here rather than in the image because the logic is plain shell and needs
no Docker daemon -- and because a broken entrypoint fails at session start,
which is the worst place to find out.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parent.parent / "containers" / "agent-entrypoint.sh"


def _run(home: Path, *command, image_agent: Path | None = None):
    """Run the entrypoint with a fake HOME, faking the image's agent binary."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
    }
    script = ENTRYPOINT.read_text()
    if image_agent is not None:
        # The real path is baked into the image; point it at a fixture instead.
        script = script.replace(
            "readonly IMAGE_AGENT=/usr/local/bin/agent", f"readonly IMAGE_AGENT={image_agent}"
        )
    # `bash -c SCRIPT a b c` binds a to $0, so the command the entrypoint should
    # exec starts at the SECOND trailing word. Without the placeholder, `exec
    # "$@"` sees `-c true` and quietly runs exec's own -c option instead.
    return subprocess.run(
        ["bash", "-c", script, "entrypoint", "bash", *command],
        capture_output=True,
        text=True,
        env=env,
    )


def test_links_the_conventional_helper_path(tmp_path):
    home = tmp_path / "bagent"
    home.mkdir()
    binary = tmp_path / "usr-local-agent"
    binary.write_text("#!/bin/sh\n")

    result = _run(home, "-c", "true", image_agent=binary)

    link = home / ".local" / "bin" / "agent"
    assert result.returncode == 0, result.stderr
    assert link.is_symlink()
    assert Path(os.readlink(link)) == binary


def test_execs_the_given_command(tmp_path):
    home = tmp_path / "bagent"
    home.mkdir()

    result = _run(home, "-c", "echo launched", image_agent=tmp_path / "x")

    assert result.returncode == 0
    assert "launched" in result.stdout


def test_replaces_a_stale_link_from_an_older_image(tmp_path):
    home = tmp_path / "bagent"
    (home / ".local" / "bin").mkdir(parents=True)
    link = home / ".local" / "bin" / "agent"
    link.symlink_to(tmp_path / "gone")
    binary = tmp_path / "new-agent"
    binary.write_text("#!/bin/sh\n")

    _run(home, "-c", "true", image_agent=binary)

    # /mnt/bagent persists across image rebuilds, so the link it carries may
    # point at a path the new image no longer has.
    assert Path(os.readlink(link)) == binary


def test_is_idempotent(tmp_path):
    home = tmp_path / "bagent"
    home.mkdir()
    binary = tmp_path / "agent"
    binary.write_text("#!/bin/sh\n")

    for _ in range(3):
        assert _run(home, "-c", "true", image_agent=binary).returncode == 0

    assert Path(os.readlink(home / ".local" / "bin" / "agent")) == binary


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the write bit")
def test_a_read_only_home_warns_but_still_starts(tmp_path):
    # A wrongly-owned mount is a provisioning problem. Saying so beats refusing
    # to start, which would leave no way to look at the mount from inside.
    home = tmp_path / "bagent"
    home.mkdir()
    home.chmod(0o500)
    try:
        result = _run(home, "-c", "echo started", image_agent=tmp_path / "x")
    finally:
        home.chmod(0o700)

    assert result.returncode == 0
    assert "started" in result.stdout
    assert "not writable" in result.stderr
    assert "ownership" in result.stderr
