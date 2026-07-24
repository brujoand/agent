from __future__ import annotations

import json
import time
import types
from pathlib import Path

import pytest

from agentcli import config, ssh
from agentcli.errors import AgentAuthError, AgentError


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # cache_dir() -> tmp so ssh_dir()/key/cert/root/meta all land under tmp.
    monkeypatch.setattr(config, "cache_dir", lambda: tmp_path / "agent")
    monkeypatch.setattr(config, "PRIVATE_ENV", tmp_path / "absent")
    monkeypatch.setenv(config.STEP_CA_PROVISIONER_PW_VAR, "s3cret")


class FakeStep:
    """Stand-in for subprocess.run that records argv and fakes step's side effects."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, capture_output=True, text=True):
        self.calls.append(argv)
        if argv[:3] == ["step", "ca", "root"]:
            Path(argv[3]).write_text("ROOTCERT")
        elif argv[:3] == ["step", "ssh", "certificate"]:
            key = Path(argv[-1])
            key.write_text("PRIVATEKEY")
            Path(f"{key}-cert.pub").write_text("CERT")
        elif argv[:1] == ["ssh-keygen"]:
            return types.SimpleNamespace(returncode=0, stdout="Valid: ...\n", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def call(self, prefix):
        return next(c for c in self.calls if c[: len(prefix)] == prefix)


@pytest.fixture
def fake_step(monkeypatch):
    fake = FakeStep()
    monkeypatch.setattr(ssh.subprocess, "run", fake)
    return fake


@pytest.mark.parametrize(
    "given,expected",
    [("1h", 3600), ("45m", 2700), ("90s", 90), ("2d", 172800), ("3600", 3600)],
)
def test_parse_ttl(given, expected):
    assert ssh._parse_ttl(given) == expected


def test_parse_ttl_rejects_garbage():
    with pytest.raises(AgentError):
        ssh._parse_ttl("soon")


def test_provisioner_password_prefers_env(monkeypatch):
    monkeypatch.setenv(config.STEP_CA_PROVISIONER_PW_VAR, "from-env")
    assert ssh._provisioner_password() == "from-env"


def test_provisioner_password_falls_back_to_private_env(monkeypatch, tmp_path):
    monkeypatch.delenv(config.STEP_CA_PROVISIONER_PW_VAR, raising=False)
    private = tmp_path / "bash_private"
    private.write_text(f'export {config.STEP_CA_PROVISIONER_PW_VAR}="from-file"\n')
    monkeypatch.setattr(config, "PRIVATE_ENV", private)
    assert ssh._provisioner_password() == "from-file"


def test_provisioner_password_missing_raises(monkeypatch):
    monkeypatch.delenv(config.STEP_CA_PROVISIONER_PW_VAR, raising=False)
    with pytest.raises(AgentAuthError):
        ssh._provisioner_password()


def test_mint_builds_step_argv_and_writes_meta(fake_step):
    before = int(time.time())
    key, cert = ssh.mint_baseline_cert("1h")

    assert key.read_text() == "PRIVATEKEY"
    assert cert.read_text() == "CERT"
    assert cert == config.ssh_cert_path()

    # Root was downloaded with fingerprint verification (no TOFU).
    root_call = fake_step.call(["step", "ca", "root"])
    assert "--fingerprint" in root_call
    assert config.STEP_CA_FINGERPRINT in root_call

    cert_call = fake_step.call(["step", "ssh", "certificate"])
    assert "--provisioner" in cert_call
    assert config.STEP_CA_PROVISIONER in cert_call
    assert cert_call[cert_call.index("--principal") + 1] == config.SSH_BASELINE_PRINCIPAL
    assert cert_call[cert_call.index("--not-after") + 1] == "1h"
    assert "--no-password" in cert_call and "--insecure" in cert_call
    # provisioner password went to a file, never the argv.
    assert "s3cret" not in cert_call

    meta = json.loads((config.ssh_dir() / "cert-meta.json").read_text())
    assert before + 3600 <= meta["expires_at"] <= int(time.time()) + 3600


def test_cert_valid_true_after_mint(fake_step):
    ssh.mint_baseline_cert("1h")
    assert ssh.cert_valid() is True


def test_cert_valid_false_when_expired(fake_step, monkeypatch):
    ssh.mint_baseline_cert("1h")
    future = time.time() + 4000  # captured before patching to avoid recursion
    monkeypatch.setattr(ssh.time, "time", lambda: future)
    assert ssh.cert_valid() is False


def test_cert_valid_false_without_cert():
    assert ssh.cert_valid() is False


def test_ensure_cert_reuses_valid_without_reminting(fake_step):
    ssh.mint_baseline_cert("1h")
    n = len(fake_step.calls)
    ssh.ensure_cert()
    assert len(fake_step.calls) == n  # no new step invocations


def test_ssh_execs_expected_argv(fake_step, monkeypatch):
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(ssh, "_exec", lambda cmd: captured.setdefault("cmd", cmd))

    ssh.ssh("chromeheim", ["uptime"])

    cmd = captured["cmd"]
    assert cmd[0] == "ssh"
    assert f"{config.AGENT_SSH_USER}@chromeheim" in cmd
    assert "-i" in cmd and str(config.ssh_key_path()) in cmd
    assert f"CertificateFile={config.ssh_cert_path()}" in cmd
    assert cmd[-1] == "uptime"
