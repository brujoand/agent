from __future__ import annotations

import io

from agentcli import credential, github


def test_get_prints_app_token(monkeypatch):
    monkeypatch.setattr(github, "token", lambda: "ghs_abc")
    out = io.StringIO()
    credential.run("get", stdin=io.StringIO("protocol=https\nhost=github.com\n\n"), stdout=out)
    assert out.getvalue() == "username=x-access-token\npassword=ghs_abc\n"


def test_get_drains_stdin_before_replying(monkeypatch):
    """git writes a key=value request then a blank line; not draining it risks SIGPIPE."""
    monkeypatch.setattr(github, "token", lambda: "ghs_abc")
    stdin = io.StringIO("protocol=https\nhost=github.com\npath=x\n\ntrailing\n")
    credential.run("get", stdin=stdin, stdout=io.StringIO())
    assert stdin.readline() == "trailing\n"


def test_store_and_erase_are_noops(monkeypatch):
    # The token is short-lived and already cached; there is nothing to persist or
    # wipe. Crucially these must not print a credential.
    monkeypatch.setattr(github, "token", lambda: "ghs_abc")
    for action in ("store", "erase"):
        out = io.StringIO()
        credential.run(action, stdin=io.StringIO("protocol=https\n\n"), stdout=out)
        assert out.getvalue() == ""


def test_store_does_not_mint(monkeypatch):
    def _boom():
        raise AssertionError("store must not mint a token")

    monkeypatch.setattr(github, "token", _boom)
    credential.run("store", stdin=io.StringIO("\n"), stdout=io.StringIO())
