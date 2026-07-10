from __future__ import annotations

import base64

import pytest

from agentcli import config, creds
from agentcli.errors import AgentAuthError

_PEM = "-----BEGIN PRIVATE KEY-----\nMIIfake\n-----END PRIVATE KEY-----\n"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in ("APP_ID", "APP_INSTALLATION_ID", "LAB_GH_APP_PRIVATE_KEY", "PEM_PATH"):
        monkeypatch.delenv(var, raising=False)
    # Never let a real ~/.bash_private leak into these tests.
    monkeypatch.setattr(creds, "PRIVATE_ENV", tmp_path / "absent")


def _set_ids(monkeypatch):
    monkeypatch.setenv("APP_ID", "123")
    monkeypatch.setenv("APP_INSTALLATION_ID", "42")


def test_inline_key_from_env(monkeypatch):
    _set_ids(monkeypatch)
    monkeypatch.setenv("LAB_GH_APP_PRIVATE_KEY", "abc")
    assert creds.load_app_creds().private_key_b64 == "abc"


def test_pem_path_supplies_a_raw_pem(monkeypatch, tmp_path):
    """The CI runner mounts the key as a read-only file, never an env var."""
    _set_ids(monkeypatch)
    pem = tmp_path / "private-key.pem"
    pem.write_text(_PEM)
    monkeypatch.setenv("PEM_PATH", str(pem))
    assert creds.load_app_creds().private_key_b64 == _PEM


def test_pem_path_supplies_a_base64_pem(monkeypatch, tmp_path):
    _set_ids(monkeypatch)
    wrapped = base64.b64encode(_PEM.encode()).decode()
    pem = tmp_path / "key.b64"
    pem.write_text(wrapped)
    monkeypatch.setenv("PEM_PATH", str(pem))
    assert creds.load_app_creds().private_key_b64 == wrapped


def test_inline_key_wins_over_pem_path(monkeypatch, tmp_path):
    _set_ids(monkeypatch)
    pem = tmp_path / "key.pem"
    pem.write_text(_PEM)
    monkeypatch.setenv("PEM_PATH", str(pem))
    monkeypatch.setenv("LAB_GH_APP_PRIVATE_KEY", "inline")
    assert creds.load_app_creds().private_key_b64 == "inline"


def test_unreadable_pem_path_fails_loudly(monkeypatch, tmp_path):
    # A silent fallthrough here would surface much later as an opaque 401 from
    # GitHub, in the one code path that has no PAT behind it.
    _set_ids(monkeypatch)
    monkeypatch.setenv("PEM_PATH", str(tmp_path / "nope.pem"))
    with pytest.raises(AgentAuthError, match="not readable"):
        creds.load_app_creds()


def test_missing_key_mentions_pem_path(monkeypatch):
    _set_ids(monkeypatch)
    with pytest.raises(AgentAuthError, match=r"PEM_PATH"):
        creds.load_app_creds()


def test_missing_ids_reported(monkeypatch, tmp_path):
    pem = tmp_path / "k.pem"
    pem.write_text(_PEM)
    monkeypatch.setenv("PEM_PATH", str(pem))
    with pytest.raises(AgentAuthError, match="APP_ID"):
        creds.load_app_creds()


def test_cache_dir_honours_xdg(monkeypatch, tmp_path):
    """A container may have a read-only HOME; XDG_CACHE_HOME must redirect the token cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert config.cache_dir() == tmp_path / "xdg" / "agent"


def test_cache_dir_defaults_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    assert config.cache_dir() == tmp_path / ".cache" / "agent"
