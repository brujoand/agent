"""Baseline SSH access via step-ca certificates.

Mints a short-lived SSH user certificate bearing the `agent-baseline` principal
and logs in as the unprivileged `brujoand-agent` user. This is the baseline path
of the agent-access system: it talks to step-ca directly with the `agent-baseline`
JWK provisioner (no broker yet). Elevated, approval-gated grants come later.

`step` (Smallstep CLI) does the crypto; we orchestrate it and cache the cert.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from agentcli import config
from agentcli.creds import read_private_var
from agentcli.errors import AgentAuthError, AgentError

_TTL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _meta_path() -> Path:
    return config.ssh_dir() / "cert-meta.json"


def _run_step(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(["step", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise AgentError(
            f"step {' '.join(args)} failed (exit {result.returncode})\n{result.stderr.strip()}"
        )
    return result


def _parse_ttl(ttl: str) -> int:
    """'1h' -> 3600, '30m' -> 1800, '90s' -> 90. Bare digits are seconds."""
    match = re.fullmatch(r"(\d+)([smhd]?)", ttl.strip())
    if not match:
        raise AgentError(f"invalid TTL {ttl!r} (want e.g. 45m, 1h, 3600)")
    value, unit = match.groups()
    return int(value) * _TTL_UNITS.get(unit, 1)


def _provisioner_password() -> str:
    pw = os.environ.get(config.STEP_CA_PROVISIONER_PW_VAR)
    if not pw:
        pw = read_private_var(config.STEP_CA_PROVISIONER_PW_VAR, config.PRIVATE_ENV)
    if not pw:
        raise AgentAuthError(
            f"{config.STEP_CA_PROVISIONER_PW_VAR} is not set.\n"
            f"  Expected in the environment or {config.PRIVATE_ENV}.\n"
            f"  A human with 1Password access provisions it: lab agent bootstrap"
        )
    return pw


def _ensure_dir() -> Path:
    directory = config.ssh_dir()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def _ensure_root_cert() -> Path:
    """Download + verify the CA root against the known fingerprint (no TOFU)."""
    root = config.step_root_path()
    if root.exists():
        return root
    _ensure_dir()
    _run_step(
        [
            "ca",
            "root",
            str(root),
            "--ca-url",
            config.STEP_CA_URL,
            "--fingerprint",
            config.STEP_CA_FINGERPRINT,
            "--force",
        ]
    )
    return root


def mint_baseline_cert(ttl: str | None = None) -> tuple[Path, Path]:
    """(Re)mint the baseline cert. Returns (key_path, cert_path)."""
    ttl = ttl or config.SSH_CERT_TTL
    lifetime = _parse_ttl(ttl)
    key = config.ssh_key_path()
    cert = config.ssh_cert_path()
    root = _ensure_root_cert()
    directory = _ensure_dir()
    password = _provisioner_password()

    # The provisioner password goes to a 0600 temp file rather than the argv, so
    # it never shows up in `ps`. Removed in the finally.
    fd, pw_path = tempfile.mkstemp(dir=directory, prefix=".pw.")
    try:
        os.write(fd, password.encode())
        os.close(fd)
        _run_step(
            [
                "ssh",
                "certificate",
                "--provisioner",
                config.STEP_CA_PROVISIONER,
                "--provisioner-password-file",
                pw_path,
                "--principal",
                config.SSH_BASELINE_PRINCIPAL,
                "--not-after",
                ttl,
                "--ca-url",
                config.STEP_CA_URL,
                "--root",
                str(root),
                "--no-password",
                "--insecure",
                "--no-agent",
                "--force",
                config.SSH_BASELINE_PRINCIPAL,  # key id
                str(key),  # private key path; cert written as <key>-cert.pub
            ]
        )
    finally:
        os.unlink(pw_path)

    key.chmod(0o600)
    _write_meta(int(time.time()) + lifetime)
    return key, cert


def _write_meta(expires_at: int) -> None:
    tmp = config.ssh_dir() / f".meta.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"expires_at": expires_at}, handle)
    tmp.replace(_meta_path())


def cert_valid(min_remaining: int = 300) -> bool:
    """True if the cached cert exists and has > min_remaining seconds left."""
    if not config.ssh_cert_path().exists():
        return False
    try:
        meta = json.loads(_meta_path().read_text())
    except (OSError, ValueError):
        return False
    return meta.get("expires_at", 0) - int(time.time()) > min_remaining


def ensure_cert(ttl: str | None = None) -> tuple[Path, Path]:
    if cert_valid():
        return config.ssh_key_path(), config.ssh_cert_path()
    return mint_baseline_cert(ttl)


def describe_cert() -> str:
    cert = config.ssh_cert_path()
    if not cert.exists():
        return "No baseline certificate yet. Run `agent access cert` or `agent ssh <host>`."
    result = subprocess.run(["ssh-keygen", "-L", "-f", str(cert)], capture_output=True, text=True)
    return result.stdout.strip() or "certificate present but unreadable"


def ssh(host: str, argv: list[str]) -> None:
    """Ensure a valid baseline cert, then exec ssh as brujoand-agent@host."""
    key, cert = ensure_cert()
    _exec(
        [
            "ssh",
            "-i",
            str(key),
            "-o",
            f"CertificateFile={cert}",
            f"{config.AGENT_SSH_USER}@{host}",
            *argv,
        ]
    )


def _exec(cmd: list[str]) -> None:
    """Replace this process with ssh (real interactive session). Patched in tests."""
    os.execvp(cmd[0], cmd)  # noqa: S606 - exec ssh directly (no shell), argv is ours
