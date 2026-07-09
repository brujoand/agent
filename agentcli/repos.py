from __future__ import annotations

import re

from agentcli import github
from agentcli.errors import AgentHTTPError

_PER_PAGE = 100
_SLUG_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)(.+?)(?:\.git)?$"
)


def clone_urls() -> list[str]:
    """HTTPS clone URLs for every repo the brujoand-agent installation can reach.

    The reachable set is decided by where the App is *installed*, not by `gh auth`
    -- the agent host has no gh login at all. So ask the installation about itself.

    HTTPS rather than SSH: the agent has no SSH key and authenticates with a
    short-lived installation token via `agent git-credential`.
    """
    urls: list[str] = []
    page = 1
    while True:
        response = github.api_get(
            "/installation/repositories", params={"per_page": _PER_PAGE, "page": page}
        )
        if response.status_code != 200:
            raise AgentHTTPError(
                "failed to list installation repositories",
                status_code=response.status_code,
                body=response.text[:500],
            )
        repositories = response.json().get("repositories", [])
        urls.extend(repo["clone_url"] for repo in repositories)
        if len(repositories) < _PER_PAGE:
            return urls
        page += 1


def slug(url: str) -> str:
    """`https://github.com/owner/name.git` -> `owner/name`.

    Normalises https and git@ forms to the same value so a clone URL and an
    existing checkout's origin compare equal.
    """
    match = _SLUG_RE.match(url.strip())
    return match.group(1) if match else url


def name(url: str) -> str:
    return slug(url).rsplit("/", 1)[-1]
