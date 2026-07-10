from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from agentcli.config import PRIVATE_ENV
from agentcli.errors import AgentAuthError

# The App id and installation id always come from the environment (or, locally,
# from ~/.bash_private). The private key has two shapes depending on where the
# CLI runs:
#
#   * on the agent host  -- LAB_GH_APP_PRIVATE_KEY, a base64-wrapped PEM baked
#     into ~/.bash_private by `lab agent bootstrap`;
#   * in the CI runner   -- PEM_PATH, pointing at the PEM that the scale-set
#     projects read-only from the brujoand-agent-credentials secret. It is
#     deliberately a file and never an env var, so the key cannot leak through
#     a process listing or an Actions secret.
#
# Either satisfies the requirement; the inline value wins if both are set.
_REQUIRED_IDS = ("APP_ID", "APP_INSTALLATION_ID")
_KEY_VAR = "LAB_GH_APP_PRIVATE_KEY"
_KEY_PATH_VAR = "PEM_PATH"
_REQUIRED = (*_REQUIRED_IDS, _KEY_VAR)


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


def _read_pem_file(path_str: str) -> str:
    path = Path(path_str)
    try:
        return path.read_text()
    except OSError as exc:
        raise AgentAuthError(f"{_KEY_PATH_VAR} is set but {path} is not readable: {exc}") from exc


def load_app_creds() -> AppCreds:
    """brujoand-agent App credentials, environment first, ~/.bash_private second.

    The private key may instead be a file named by PEM_PATH -- that is how the CI
    runner receives it, projected read-only from a Kubernetes secret. An inline
    LAB_GH_APP_PRIVATE_KEY wins if both are present.

    There is no 1Password fallback: the agent host has no OP_SERVICE_ACCOUNT_TOKEN
    and never will. ~/.bash_private is placed out-of-band by `lab agent bootstrap`,
    run by a human who does have op. That file is the entire contract between lab
    and agent.
    """
    values = {k: os.environ[k] for k in _REQUIRED if os.environ.get(k)}

    if len(values) < len(_REQUIRED):
        values = {**_parse_private_env(PRIVATE_ENV), **values}

    # Only consult PEM_PATH once the inline key has had its chance: a container
    # that sets both should behave predictably, and env-wins matches how the ids
    # already resolve. github._load_pem accepts a raw PEM, so the file's contents
    # flow through unchanged.
    if not values.get(_KEY_VAR) and os.environ.get(_KEY_PATH_VAR):
        values[_KEY_VAR] = _read_pem_file(os.environ[_KEY_PATH_VAR])

    missing = [k for k in _REQUIRED if not values.get(k)]
    if missing:
        hint = _KEY_PATH_VAR if _KEY_VAR in missing else ""
        raise AgentAuthError(
            f"missing App credentials: {', '.join(missing)}\n"
            f"  Expected in the environment or {PRIVATE_ENV}"
            + (f" (or a PEM at ${hint})" if hint else "")
            + ".\n"
            f"  A human with 1Password access provisions them: lab agent bootstrap {PRIVATE_ENV.parent}"
        )

    return AppCreds(
        app_id=values["APP_ID"],
        installation_id=values["APP_INSTALLATION_ID"],
        private_key_b64=values["LAB_GH_APP_PRIVATE_KEY"],
    )
