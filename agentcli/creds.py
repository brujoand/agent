from __future__ import annotations

import os
import shlex
from dataclasses import dataclass

from agentcli.config import PRIVATE_ENV
from agentcli.errors import AgentAuthError

_REQUIRED = ("APP_ID", "APP_INSTALLATION_ID", "LAB_GH_APP_PRIVATE_KEY")


@dataclass(frozen=True)
class AppCreds:
    app_id: str
    installation_id: str
    private_key_b64: str


def _parse_private_env(path) -> dict[str, str]:
    """Read `export KEY=value` lines out of ~/.bash_private.

    Parsed rather than sourced: git spawns `agent git-credential` as a bare
    subprocess, so we cannot rely on a login shell having exported anything, and
    shelling out to bash just to read three variables would be silly. `lab agent
    bootstrap` writes the values with printf %q, so shlex handles the quoting.
    """
    found: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return found

    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("export "):
            continue
        assignment = line[len("export ") :]
        key, sep, value = assignment.partition("=")
        if not sep or key not in _REQUIRED:
            continue
        parts = shlex.split(value)
        if parts:
            found[key] = parts[0]
    return found


def load_app_creds() -> AppCreds:
    """brujoand-agent App credentials, environment first, ~/.bash_private second.

    There is no 1Password fallback: the agent host has no OP_SERVICE_ACCOUNT_TOKEN
    and never will. ~/.bash_private is placed out-of-band by `lab agent bootstrap`,
    run by a human who does have op. That file is the entire contract between lab
    and agent.
    """
    values = {k: os.environ[k] for k in _REQUIRED if os.environ.get(k)}

    if len(values) < len(_REQUIRED):
        values = {**_parse_private_env(PRIVATE_ENV), **values}

    missing = [k for k in _REQUIRED if not values.get(k)]
    if missing:
        raise AgentAuthError(
            f"missing App credentials: {', '.join(missing)}\n"
            f"  Expected in the environment or {PRIVATE_ENV}.\n"
            f"  A human with 1Password access provisions them: lab agent bootstrap {PRIVATE_ENV.parent}"
        )

    return AppCreds(
        app_id=values["APP_ID"],
        installation_id=values["APP_INSTALLATION_ID"],
        private_key_b64=values["LAB_GH_APP_PRIVATE_KEY"],
    )
