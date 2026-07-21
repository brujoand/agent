from __future__ import annotations

import base64
import json
import os
import random
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from agentcli.config import GITHUB_API, cache_dir
from agentcli.creds import AppCreds, load_app_creds
from agentcli.errors import AgentAuthError

# SIBLING IMPLEMENTATION -- keep in sync with
# gitops-homelab:containers/github-runner/mint-app-token.sh
#
# That bash script is baked into the CI runner image and called by six workflows.
# It cannot import this module (the runner has no access to this private repo),
# and this module cannot call it (agent bootstraps *before* gitops-homelab is on
# disk). So the mint exists twice, deliberately. The parts that must not drift:
#   * iat back-dated 300s -- a host clock ahead of GitHub's gets 401 "'iat' is in
#     the future"; exp +540s keeps the assertion inside GitHub's 10-minute cap.
#   * transient (5xx / 429 / transport) retries with exponential backoff+jitter;
#   * any OTHER 4xx fails fast -- a bad JWT, wrong installation id, or revoked
#     App will never recover, and retrying only delays a doomed job.

_JWT_BACKDATE_SECONDS = 300
_JWT_LIFETIME_SECONDS = 540
_MAX_ATTEMPTS = 5
_MAX_BACKOFF_SECONDS = 30

# Installation tokens last ~1h. Reuse a cached one only while it has real life
# left, so a long `agent pull` never dies mid-clone holding an expired token.
_EXPIRY_MARGIN_SECONDS = 300
_ASSUMED_LIFETIME_SECONDS = 3600


def _cache_file() -> Path:
    return cache_dir() / "github-app-token.json"


def _load_pem(private_key_b64: str) -> bytes:
    """Accept a base64-wrapped PEM (how bootstrap bakes it) or a raw PEM."""
    raw = private_key_b64.encode()
    try:
        decoded = base64.b64decode(raw, validate=True)
        if decoded.lstrip().startswith(b"-----BEGIN"):
            return decoded
    except (ValueError, TypeError):
        pass

    if raw.lstrip().startswith(b"-----BEGIN"):
        return raw

    raise AgentAuthError("LAB_GH_APP_PRIVATE_KEY is neither a raw nor a base64-wrapped PEM")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign_jwt(creds: AppCreds, now: int | None = None) -> str:
    now = int(time.time()) if now is None else now
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": now - _JWT_BACKDATE_SECONDS,
        "exp": now + _JWT_LIFETIME_SECONDS,
        "iss": creds.app_id,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    ).encode()

    key = serialization.load_pem_private_key(_load_pem(creds.private_key_b64), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode()}.{_b64url(signature)}"


def _is_retryable(status: int) -> bool:
    """429 and 5xx recover; every other 4xx is permanent."""
    return status == 429 or status >= 500


def _sleep_backoff(attempt: int) -> None:
    backoff = min(2 ** (attempt - 1), _MAX_BACKOFF_SECONDS)
    time.sleep(backoff + random.random())  # noqa: S311 - jitter, not crypto


def mint(creds: AppCreds | None = None, repositories: list[str] | None = None) -> tuple[str, int]:
    """Exchange a signed JWT for an installation token. Returns (token, expires_at).

    ``repositories`` (repo *names*, not owner/repo slugs) narrows the token to just
    those repos in the installation instead of all of them — used by the hub so a
    run on one repo cannot touch the others (least privilege / blast-radius limit).
    """
    creds = creds or load_app_creds()
    jwt = _sign_jwt(creds)
    url = f"{GITHUB_API}/app/installations/{creds.installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = {"repositories": repositories} if repositories else None

    last = "unknown error"
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = httpx.post(url, headers=headers, json=body, timeout=20.0)
        except httpx.HTTPError as exc:  # transport: timeout, DNS, connection reset
            last = f"transport error: {exc}"
            if attempt < _MAX_ATTEMPTS:
                _sleep_backoff(attempt)
            continue

        if response.status_code == 201:
            return response.json()["token"], int(time.time()) + _ASSUMED_LIFETIME_SECONDS

        try:
            message = response.json().get("message", "?")
        except ValueError:
            message = response.text[:200]
        last = f"HTTP {response.status_code}: {message}"

        if not _is_retryable(response.status_code):
            raise AgentAuthError(f"mint failed, non-retryable ({last})")

        if attempt < _MAX_ATTEMPTS:
            _sleep_backoff(attempt)

    raise AgentAuthError(f"mint failed after {_MAX_ATTEMPTS} attempts ({last})")


def _read_cache() -> str | None:
    try:
        data = json.loads(_cache_file().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    token = data.get("token")
    expires_at = data.get("expires_at", 0)
    if token and expires_at > time.time() + _EXPIRY_MARGIN_SECONDS:
        return token
    return None


def _write_cache(token: str, expires_at: int) -> None:
    directory = cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)

    # Create at 0600 rather than write-then-chmod: no window in which the token
    # is world-readable. Written to a temp file and renamed so a concurrent
    # reader never sees a partial file.
    tmp = directory / f".tok.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"token": token, "expires_at": expires_at}, handle)
    tmp.replace(_cache_file())


def token(force: bool = False, repositories: list[str] | None = None) -> str:
    """A valid installation token, cached until shortly before it expires.

    A repo-scoped token (``repositories`` given) is always minted fresh and never
    cached — the cache holds the broad installation-wide token, and mixing a
    narrowly-scoped one into it would hand later callers the wrong scope.
    """
    if repositories:
        fresh, _ = mint(repositories=repositories)
        return fresh

    if not force:
        cached = _read_cache()
        if cached:
            return cached

    fresh, expires_at = mint()
    _write_cache(fresh, expires_at)
    return fresh


def token_expires_in() -> int:
    """Seconds of life left on the cached token, or 0 if there is none."""
    try:
        data = json.loads(_cache_file().read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    return max(0, int(data.get("expires_at", 0) - time.time()))


def _api_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def api_get(path: str, params: dict | None = None) -> httpx.Response:
    return httpx.get(f"{GITHUB_API}{path}", headers=_api_headers(), params=params, timeout=20.0)


def api_post(path: str, json_body: dict | None = None) -> httpx.Response:
    return httpx.post(f"{GITHUB_API}{path}", headers=_api_headers(), json=json_body, timeout=20.0)


def api_put(path: str, json_body: dict | None = None) -> httpx.Response:
    return httpx.put(f"{GITHUB_API}{path}", headers=_api_headers(), json=json_body, timeout=20.0)


def app_slug() -> str:
    """The agent App's own slug (e.g. ``my-agent``) — the bare login its comments
    carry. Signs an App JWT and reads ``GET /app`` (an App-level endpoint, so a
    JWT, not the installation token). Lets `agent issue enable` auto-fill the
    runtime's AGENT_BOT_LOGIN and the summon @mention, so a consumer never has to
    hand-configure them."""
    jwt = _sign_jwt(load_app_creds())
    response = httpx.get(
        f"{GITHUB_API}/app",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=20.0,
    )
    if response.status_code != 200:
        raise AgentAuthError(
            f"could not read /app to detect the App slug (HTTP {response.status_code})"
        )
    return response.json()["slug"]
