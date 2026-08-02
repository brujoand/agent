"""Git helpers shared across the CLI.

`slug` has two callers -- `inflight` (which repo's PRs to ask about) and
`workspace` (which repo's merged branches to collect) -- so it is tested once,
here, rather than once per caller.
"""

import subprocess
from pathlib import Path

import pytest

from agentcli import git


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/brujoand/agent.git", "brujoand/agent"),
        ("https://github.com/brujoand/agent", "brujoand/agent"),
        ("git@github.com:brujoand/agent.git", "brujoand/agent"),
        ("git@github.com:brujoand/agent", "brujoand/agent"),
        ("https://github.com/brujoand/agent/", "brujoand/agent"),
    ],
)
def test_slug_parses_every_remote_url_form(monkeypatch, url, expected):
    monkeypatch.setattr(
        git.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, url + "\n", "")
    )
    assert git.slug(Path("/anywhere")) == expected


def test_slug_is_none_without_a_remote(monkeypatch):
    monkeypatch.setattr(
        git.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    assert git.slug(Path("/anywhere")) is None


def test_slug_is_none_for_an_unparseable_remote(monkeypatch):
    monkeypatch.setattr(
        git.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "agent\n", "")
    )
    assert git.slug(Path("/anywhere")) is None
