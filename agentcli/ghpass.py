from __future__ import annotations

import os
import shutil

from agentcli import github
from agentcli.errors import AgentError


def exec_gh(args: list[str]) -> None:
    """Run `gh` with a fresh agent App token in the environment, then hand over.

    This is the agentic entry point to the GitHub CLI: it mints a token via the
    App and exports it as GH_TOKEN before gh sees it, so an agent can run
    `agent gh pr view 1337` instead of the noisy
    `GH_TOKEN=$(agent github token) gh pr view 1337`. gh owns the flag grammar,
    the terminal, the exit code, and any signals.

    The binary is resolved on PATH (gh is not one of ours, so there is no wrapper
    to recurse into), and the token is minted only after gh is found so a missing
    gh never triggers a needless mint. execve replaces this process, leaving no
    wrapper in the middle to swallow gh's output or status.
    """
    binary = shutil.which("gh")
    if binary is None:
        raise AgentError("gh CLI not found on PATH")

    env = dict(os.environ)
    env["GH_TOKEN"] = github.token()

    # S606: no shell, and the target is a resolved absolute path, not user input.
    os.execve(binary, ["gh", *args], env)  # noqa: S606
