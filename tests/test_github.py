from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agentcli import github
from agentcli.creds import AppCreds
from agentcli.errors import AgentAuthError

_TOKEN_URL = "https://api.github.com/app/installations/42/access_tokens"


@pytest.fixture
def pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture
def creds(pem: bytes) -> AppCreds:
    return AppCreds("123", "42", base64.b64encode(pem).decode())


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(github, "cache_dir", lambda: tmp_path / "agent")
    monkeypatch.setattr(github.time, "sleep", lambda _s: None)


def test_load_pem_accepts_base64_and_raw(pem: bytes):
    assert github._load_pem(base64.b64encode(pem).decode()) == pem
    assert github._load_pem(pem.decode()) == pem


def test_load_pem_rejects_garbage():
    with pytest.raises(AgentAuthError, match="neither a raw nor a base64"):
        github._load_pem("not-a-key")


def test_jwt_backdates_iat_and_bounds_exp(creds: AppCreds):
    now = 1_000_000
    payload = json.loads(
        base64.urlsafe_b64decode(github._sign_jwt(creds, now=now).split(".")[1] + "==")
    )
    # A host clock ahead of GitHub's gets 401 "'iat' is in the future" without the
    # backdate. GitHub caps exp at 10 minutes from NOW (not from iat) -- which is
    # why exp - iat legitimately exceeds 600s here.
    assert payload["iat"] == now - 300
    assert payload["exp"] == now + 540
    assert payload["exp"] - now <= 600
    assert payload["iss"] == "123"


def test_mint_returns_token(httpx_mock, creds: AppCreds):
    httpx_mock.add_response(url=_TOKEN_URL, status_code=201, json={"token": "ghs_abc"})
    token, expires_at = github.mint(creds)
    assert token == "ghs_abc"
    assert expires_at > time.time()


def test_mint_retries_transient_then_succeeds(httpx_mock, creds: AppCreds):
    httpx_mock.add_response(url=_TOKEN_URL, status_code=500, json={"message": "boom"})
    httpx_mock.add_response(url=_TOKEN_URL, status_code=429, json={"message": "slow down"})
    httpx_mock.add_response(url=_TOKEN_URL, status_code=201, json={"token": "ghs_ok"})
    assert github.mint(creds)[0] == "ghs_ok"


def test_mint_fast_fails_on_401_without_retrying(httpx_mock, creds: AppCreds):
    # A bad JWT / wrong installation / revoked App never recovers. Retrying only
    # delays a doomed job -- so exactly one request must be made.
    httpx_mock.add_response(url=_TOKEN_URL, status_code=401, json={"message": "bad jwt"})
    with pytest.raises(AgentAuthError, match="non-retryable"):
        github.mint(creds)
    assert len(httpx_mock.get_requests()) == 1


def test_mint_gives_up_after_max_attempts(httpx_mock, creds: AppCreds):
    for _ in range(github._MAX_ATTEMPTS):
        httpx_mock.add_response(url=_TOKEN_URL, status_code=503, json={"message": "down"})
    with pytest.raises(AgentAuthError, match="after 5 attempts"):
        github.mint(creds)
    assert len(httpx_mock.get_requests()) == github._MAX_ATTEMPTS


def test_cache_is_written_0600_and_reused(monkeypatch):
    monkeypatch.setattr(github, "mint", lambda creds=None: ("ghs_cached", int(time.time()) + 3600))
    assert github.token() == "ghs_cached"
    assert github._cache_file().stat().st_mode & 0o777 == 0o600

    # A second call must not mint again.
    monkeypatch.setattr(
        github, "mint", lambda creds=None: pytest.fail("should have used the cache")
    )
    assert github.token() == "ghs_cached"


def test_cache_ignored_inside_expiry_margin(monkeypatch):
    # 4 minutes of life left is inside the 5-minute margin: a long `agent pull`
    # must not start a clone holding a token that dies mid-transfer.
    github._write_cache("ghs_stale", int(time.time()) + 240)
    monkeypatch.setattr(github, "mint", lambda creds=None: ("ghs_fresh", int(time.time()) + 3600))
    assert github.token() == "ghs_fresh"


def test_force_refresh_bypasses_valid_cache(monkeypatch):
    github._write_cache("ghs_valid", int(time.time()) + 3600)
    monkeypatch.setattr(github, "mint", lambda creds=None: ("ghs_forced", int(time.time()) + 3600))
    assert github.token(force=True) == "ghs_forced"


def test_repo_scoped_token_sends_repositories_and_skips_cache(monkeypatch, httpx_mock, creds):
    # A valid installation-wide token is cached...
    github._write_cache("ghs_broad", int(time.time()) + 3600)
    monkeypatch.setattr(github, "load_app_creds", lambda: creds)
    httpx_mock.add_response(url=_TOKEN_URL, status_code=201, json={"token": "ghs_scoped"})

    # ...but a repo-scoped request mints fresh (never the broad cached one) and
    # narrows the token to just that repo.
    assert github.token(repositories=["tracktor"]) == "ghs_scoped"
    assert json.loads(httpx_mock.get_requests()[0].content) == {"repositories": ["tracktor"]}


def test_api_post_sends_json_body_with_bearer(monkeypatch, httpx_mock):
    monkeypatch.setattr(github, "token", lambda: "ghs_api")
    httpx_mock.add_response(
        method="POST",
        url="https://api.github.com/repos/brujoand/x/labels",
        status_code=201,
        json={"name": "agent"},
    )
    resp = github.api_post("/repos/brujoand/x/labels", {"name": "agent"})
    assert resp.status_code == 201
    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == "Bearer ghs_api"
    assert json.loads(request.content) == {"name": "agent"}


def test_api_put_sends_json_body(monkeypatch, httpx_mock):
    monkeypatch.setattr(github, "token", lambda: "ghs_api")
    httpx_mock.add_response(
        method="PUT",
        url="https://api.github.com/repos/brujoand/x/contents/f",
        status_code=200,
        json={"content": {}},
    )
    resp = github.api_put("/repos/brujoand/x/contents/f", {"message": "m"})
    assert resp.status_code == 200
    assert json.loads(httpx_mock.get_requests()[0].content) == {"message": "m"}


def test_app_slug_reads_app_endpoint(monkeypatch, httpx_mock, creds):
    # Signs an App JWT (not the installation token) and returns the App's slug.
    monkeypatch.setattr(github, "load_app_creds", lambda: creds)
    httpx_mock.add_response(
        method="GET",
        url="https://api.github.com/app",
        status_code=200,
        json={"slug": "my-agent"},
    )
    assert github.app_slug() == "my-agent"
    assert httpx_mock.get_requests()[0].headers["Authorization"].startswith("Bearer ")


def test_app_slug_raises_on_error(monkeypatch, httpx_mock, creds):
    monkeypatch.setattr(github, "load_app_creds", lambda: creds)
    httpx_mock.add_response(method="GET", url="https://api.github.com/app", status_code=403)
    with pytest.raises(AgentAuthError):
        github.app_slug()
