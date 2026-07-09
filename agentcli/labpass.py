from __future__ import annotations

import os
from pathlib import Path

from agentcli import github
from agentcli.config import DEFAULT_REPO
from agentcli.install import lab_binary


def exec_lab(args: list[str], repo: str = DEFAULT_REPO) -> None:
    """Run `lab` with agent credentials in the environment, then hand over the process.

    This is the agentic entry point to lab: it guarantees a fresh App token and a
    kubeconfig before lab sees them, so no lab module has to know how the agent
    authenticates. A human running bare `lab` is unaffected.

    execv replaces this process, so lab owns the terminal, the exit code, and any
    signals -- there is no wrapper left in the middle to swallow them. The target
    is the concrete binary under the agent root, never a PATH lookup: `agent` and
    `lab` both live in ~/.local/bin, and resolving by name risks recursing into a
    wrapper.
    """
    binary = lab_binary(repo)

    env = dict(os.environ)
    env["GH_TOKEN"] = github.token()
    env.setdefault("KUBECONFIG", str(Path.home() / ".kube" / "config"))

    # S606: no shell, and the target is a resolved absolute path, not user input.
    os.execve(str(binary), ["lab", *args], env)  # noqa: S606
