from __future__ import annotations

import pytest

from agentcli import repos
from agentcli.errors import AgentHTTPError

_URL = "https://api.github.com/installation/repositories"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr("agentcli.github.token", lambda: "ghs_test")


def _page(n: int) -> dict:
    return {
        "repositories": [{"clone_url": f"https://github.com/brujoand/r{i}.git"} for i in range(n)]
    }


def test_single_page(httpx_mock):
    httpx_mock.add_response(url=f"{_URL}?per_page=100&page=1", json=_page(3))
    assert len(repos.clone_urls()) == 3


def test_paginates_until_short_page(httpx_mock):
    httpx_mock.add_response(url=f"{_URL}?per_page=100&page=1", json=_page(100))
    httpx_mock.add_response(url=f"{_URL}?per_page=100&page=2", json=_page(7))
    assert len(repos.clone_urls()) == 107


def test_non_200_raises(httpx_mock):
    httpx_mock.add_response(url=f"{_URL}?per_page=100&page=1", status_code=403, text="nope")
    with pytest.raises(AgentHTTPError, match="failed to list"):
        repos.clone_urls()


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/brujoand/agent.git",
        "https://github.com/brujoand/agent",
        "git@github.com:brujoand/agent.git",
        "ssh://git@github.com/brujoand/agent.git",
    ],
)
def test_slug_normalises_every_remote_form(url):
    # An https clone URL and a git@ origin for the same repo must compare equal,
    # or `agent pull` would fail to recognise its own checkout.
    assert repos.slug(url) == "brujoand/agent"
    assert repos.name(url) == "agent"
