from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agentcli.config import DEFAULT_REPO, repo_path
from agentcli.errors import AgentConfigError

_MISE = Path.home() / ".local" / "bin" / "mise"
_BIN_DIR = Path.home() / ".local" / "bin"

# Pinned in gitops-homelab's mise.toml, but only directory-scoped there; mirror
# them into the global mise config so kubectl/talosctl resolve from any cwd.
_GLOBAL_TOOLS = ("kubectl", "talosctl", "node", "gh", "helm", "flux2", "kustomize", "yq", "uv")


def _mise(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(_MISE), *args], cwd=cwd, check=True, text=True)


def run(repo: str = DEFAULT_REPO) -> int:
    """Install the `lab` CLI from the checkout under the agent root.

    Runs after `agent pull`, which is what puts gitops-homelab on disk. The
    dependency points one way: the agent root knows how to install lab; lab knows
    nothing about the agent root.
    """
    checkout = repo_path(repo)
    if not (checkout / ".git").exists():
        raise AgentConfigError(f"{checkout} is not a git checkout -- run `agent pull` first")
    if not _MISE.is_file():
        raise AgentConfigError(f"mise not found at {_MISE}")

    print("==> installing repo-pinned toolchain (mise)")
    _mise(["trust", str(checkout)])
    _mise(["install"], cwd=checkout)

    print("==> pinning cluster CLIs globally")
    for tool in _GLOBAL_TOOLS:
        result = subprocess.run(
            [str(_MISE), "current", tool], cwd=checkout, capture_output=True, text=True
        )
        version = result.stdout.strip()
        if result.returncode == 0 and version:
            _mise(["use", "-g", f"{tool}@{version}"])

    # Without the venv, lab/lab quietly falls back to its bash dispatcher and its
    # Python-backed modules vanish with no error. Build it explicitly.
    print("==> building the lab CLI venv (uv sync)")
    _mise(["exec", "--", "uv", "sync"], cwd=checkout / "lab")

    print(f"==> symlinking lab into {_BIN_DIR}")
    _BIN_DIR.mkdir(parents=True, exist_ok=True)
    link = _BIN_DIR / "lab"
    target = checkout / "lab" / "lab"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)

    print(f"\ninstall: lab installed from {checkout}")
    print(f"  {os.path.realpath(link)}")
    return 0


def lab_binary(repo: str = DEFAULT_REPO) -> Path:
    """The concrete lab executable, resolved under the agent root.

    Never `~/.local/bin/lab` and never a PATH lookup: `agent lab` execs this, and
    a PATH lookup could resolve back to a wrapper and recurse.
    """
    binary = repo_path(repo) / "lab" / "lab"
    if not binary.is_file():
        raise AgentConfigError(
            f"lab not found at {binary} -- run `agent pull && agent lab install`"
        )
    return binary
