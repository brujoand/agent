"""Shared test setup.

issue_agent/ runs in production as a flat script directory (`python
/opt/issue-agent/agent.py` puts it on sys.path[0]); replicate exactly that so
`agent`, `providers`, and `s3_session_store` import as top-level modules here
too, instead of inventing a package shape production doesn't have.
"""

import sys
from pathlib import Path

import pytest

from agentcli import creds

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "issue_agent"))


@pytest.fixture(autouse=True)
def _no_ambient_app_credentials(monkeypatch, tmp_path):
    """Run every test as though the machine has no App credentials.

    `creds.load_app_creds` reads the environment first and `~/.bash_private`
    second, and on the agent host that file always exists. So a test that
    forgets to stub a GitHub call does not fail there -- it resolves real
    credentials, mints a real token, and makes a REAL API call, quietly passing.
    The same test then fails on CI, where nothing is provisioned, and the error
    ("missing App credentials") points at the environment rather than at the
    missing stub. That happened: four `issue_enable.run` tests reached the live
    API for months and only broke when `run` started calling `is_public`.

    Clearing the variables is not enough on its own, because of `~/.bash_private`
    -- HOME has to move too. Both are undone after each test by monkeypatch.

    HOME goes to a SUBDIRECTORY of tmp_path, never tmp_path itself: tests that
    build a tree there (tests/test_rules.py, tests/test_hooks.py) would otherwise
    find their fixture paths sitting under home, and any code that abbreviates a
    path to `~/...` would rewrite them out from under the assertions.

    This does not stop a test from setting its own credentials: an autouse
    fixture runs before the test body, so any `monkeypatch.setenv` inside a test
    still wins. It only removes what the *machine* happens to be carrying.
    """
    for var in ("APP_ID", "APP_INSTALLATION_ID", "LAB_GH_APP_PRIVATE_KEY", "PEM_PATH"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "_home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    # And the token cache, which `token()` consults BEFORE minting. It lives at
    # `cache_dir()`, which prefers XDG_CACHE_HOME over ~/.cache -- so moving HOME
    # alone leaves a real, still-valid installation token sitting there for any
    # unstubbed call to pick up. That is the loophole the four `run` tests fell
    # through: no credentials needed, because a live token was already cached.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "_cache"))
    # `PRIVATE_ENV` is `Path.home() / ".bash_private"` evaluated at IMPORT time
    # (agentcli/config.py), so it is already bound to the real home before any
    # fixture runs -- moving HOME does not move it. Rebind it where creds.py
    # actually looks it up.
    monkeypatch.setattr(creds, "PRIVATE_ENV", home / ".bash_private", raising=False)
