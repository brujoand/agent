"""What a session may NOT do, whatever it was allowed to do.

The issue/PR agent reads attacker-controlled text: issue bodies, PR
descriptions, and thread comments all reach the model, and the model holds a
GitHub App installation token whose reach is every repo the App is installed on.
So the interesting question is not "what should it be able to do" but "what must
stay impossible even when the text tells it otherwise".

Two layers, because neither is sufficient alone.

**Deny rules** (``DENIED_TOOLS``, passed as the SDK's ``disallowed_tools``) are
evaluated before allow rules and win unconditionally -- a deny beats
``allowed_tools`` and beats ``permission_mode``. They are the cheap, declarative
half.

**A PreToolUse hook** (``denial_reason``) is the half that actually holds. Deny
rules match command *patterns*, and Claude Code's own guidance is explicit that
command parsing for permissions is a permission gate, not a sandbox: a shell
hides behaviour through pipes, subshells, redirects, aliases, variables and
generated scripts, so ``Bash(gh auth:*)`` does not stop ``bash -c 'gh auth
token'``. This module therefore inspects the RAW command string for the
dangerous substrings wherever they appear, rather than trusting position.

Note what that buys and what it does not. A PreToolUse hook fires for every tool
call, including ones an allow rule already approved (``can_use_tool`` does NOT
-- auto-approved tools skip it entirely, which is why the chokepoint is here).
But a determined multi-step exfiltration -- write a secret to a file in one
call, publish the file in another -- is not preventable by inspecting single
commands. This is defence in depth against a prompt-injected model, not a
sandbox. The real boundary is that the runner is ephemeral and the token is
short-lived; keep it that way.

Deliberately deployment-neutral: every host, path and branch comes from the
environment, so nothing here names one setup. This repo is public.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Env vars whose VALUES are credentials. Naming one in a command that could
# print or transmit it is treated as exfiltration.
SECRET_ENV_VARS = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
)

# Tool-pattern denials. Evaluated before allow rules and unconditional -- these
# hold even though _COMMON_TOOLS allows `Bash(gh:*)` wholesale. `//` prefixes an
# absolute filesystem path (a single `/` would anchor at the session cwd).
DENIED_TOOLS = [
    "Read(//run/agent/**)",  # the App private key mount
    "Read(//proc/self/environ)",  # the process env, tokens included
    "Bash(gh auth:*)",  # `gh auth token` prints the installation token
    "Bash(gh secret:*)",
    "Bash(gh variable:*)",
]


def pem_path() -> str:
    """Where the App private key is mounted. Matches the workflows' PEM_PATH."""
    return os.environ.get("PEM_PATH", "/run/agent/private-key.pem")


def protected_paths() -> list[str]:
    """Absolute paths no session may read, by any means."""
    pem = pem_path()
    return [pem, str(Path(pem).parent), "/proc/self/environ"]


def base_branch() -> str:
    """The branch a PR targets -- and therefore must never be pushed to."""
    return os.environ.get("AGENT_BASE_BRANCH") or "main"


def _egress_allow_hosts() -> set[str]:
    """Hosts curl/wget may reach, from AGENT_EGRESS_ALLOW_HOSTS.

    Empty by default: with no allowlist configured, no arbitrary egress is
    permitted at all. A deployment that turns on cluster reads names its own
    hosts, so no internal hostname is ever baked in here.
    """
    raw = os.environ.get("AGENT_EGRESS_ALLOW_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


# `git push` variants that rewrite history or bypass a rejected update.
_FORCE_PUSH = re.compile(r"(?:^|\s)(?:--force\b|--force-with-lease|--force-if-includes|-f\b)")
# A refspec whose source is prefixed with `+` forces without naming --force.
_FORCE_REFSPEC = re.compile(r"(?:^|\s)\+[^\s:]+:")
# `env`/`printenv` used to DUMP rather than to set a variable for one command.
# `env FOO=bar cmd` is ordinary and stays allowed; bare `env` is a dump.
_ENV_DUMP = re.compile(r"(?:^|[;&|(]\s*)(?:printenv\b|env\b(?!\s+[\w.]+=))")
# Commands that put their argument somewhere a human or a network can see it.
# `--body`/`comment` matter most: posting to the thread is the shortest path
# from a credential to an attacker, and needs no network tool at all.
_PUBLISHES = re.compile(
    r"\b(?:echo|printf|cat|curl|wget|nc|ncat|mail|base64|xxd|tee|comment)\b|--body"
)


def _hosts_in(command: str) -> list[str]:
    """Hostnames appearing as URLs in a command."""
    return [m.group(1).lower() for m in re.finditer(r"https?://([^/\s'\"]+)", command)]


def _mentions_secret_path(command: str) -> str | None:
    for path in protected_paths():
        if path and path in command:
            return path
    # Any private-key-shaped file, wherever a deployment mounts it.
    if re.search(r"[\w./-]+\.(?:pem|key)\b", command) or re.search(
        r"\bid_(?:rsa|ed25519)\b", command
    ):
        return "a private-key file"
    return None


def _push_segments(command: str) -> list[str]:
    """The `git ... push ...` runs of a command, split on shell separators.

    Scoped rather than whole-string so a push followed by something unrelated is
    judged on the push alone: `git push origin feat/x && rm -f tmp` must not read
    as a force push, and `... && echo main` must not read as a push to the base
    branch.
    """
    return [seg for seg in re.split(r"[;&|]+", command) if re.search(r"\bgit\b.*\bpush\b", seg)]


def _pushes_to_base(command: str) -> bool:
    """Does this look like `git push` aimed at the protected base branch?"""
    base = base_branch()
    # `git push origin main`, `git push origin HEAD:main`, `git push -u origin main`
    return any(
        re.search(rf"(?:^|[\s:]){re.escape(base)}(?:\s|$|:)", seg)
        for seg in _push_segments(command)
    )


def bash_denial_reason(command: str) -> str | None:
    """Why this shell command must not run, or None to allow it.

    Matches substrings anywhere rather than at command position, because the
    threat model is a model following injected instructions -- which will not
    politely put `gh auth token` first on the line.
    """
    if not command or not command.strip():
        return None

    path = _mentions_secret_path(command)
    if path:
        return (
            f"Reads or handles {path}. The App private key and other credentials are "
            "off limits to the session; nothing in an issue thread is a reason to touch them."
        )

    if re.search(r"\bgh\s+auth\b", command):
        return (
            "`gh auth` can print the installation token, which would publish a credential "
            "reaching every repo this App is installed on. Use `gh` subcommands directly; "
            "authentication is already configured."
        )

    if re.search(r"\bgh\s+(?:secret|variable)\b", command):
        return "`gh secret` / `gh variable` manage repository credentials, which the agent may not touch."

    if _ENV_DUMP.search(command):
        return (
            "Dumping the environment would expose GITHUB_TOKEN and CLAUDE_CODE_OAUTH_TOKEN. "
            "Read the specific variable you need instead of the whole environment."
        )

    named = [v for v in SECRET_ENV_VARS if v in command]
    if named and _PUBLISHES.search(command):
        return (
            f"References {', '.join(named)} in a command that could print or transmit it. "
            "Credentials must never reach a comment, a file, or the network."
        )

    if any(
        _FORCE_PUSH.search(seg) or _FORCE_REFSPEC.search(seg) for seg in _push_segments(command)
    ):
        return "Force-pushing is never allowed: it rewrites history the maintainer has not seen."

    if _pushes_to_base(command):
        return (
            f"Pushing to '{base_branch()}' is never allowed. Push a feature branch and open a PR; "
            "only a human merges."
        )

    hosts = _hosts_in(command)
    if hosts and re.search(r"\b(?:curl|wget|nc|ncat)\b", command):
        allowed = _egress_allow_hosts() | {
            "api.github.com",
            "github.com",
            "raw.githubusercontent.com",
        }
        blocked = [h for h in hosts if h.split(":")[0] not in allowed]
        if blocked:
            return (
                f"Network egress to {', '.join(sorted(set(blocked)))} is not permitted. "
                "Arbitrary egress is an exfiltration channel; allowed hosts come from "
                "AGENT_EGRESS_ALLOW_HOSTS."
            )

    return None


def _outside(path_str: str, cwd: str) -> bool:
    """Is this an absolute path outside the session's working tree?"""
    if not path_str:
        return False
    try:
        target = Path(path_str).expanduser()
        if not target.is_absolute():
            return False
        return not str(target.resolve()).startswith(str(Path(cwd).resolve()))
    except (OSError, RuntimeError, ValueError):
        return True  # unresolvable -> treat as outside


def file_denial_reason(tool_name: str, path_str: str, cwd: str) -> str | None:
    """Why this file access must not happen, or None to allow it."""
    for protected in protected_paths():
        if protected and path_str.startswith(protected):
            return f"{protected} holds credentials and is off limits to the session."
    if tool_name in ("Write", "Edit", "NotebookEdit") and _outside(path_str, cwd):
        return (
            f"Writing outside the checkout ({cwd}) is not permitted. The session's job is to "
            "change the repository, and every change lands through a pull request."
        )
    return None


def denial_reason(tool_name: str, tool_input: dict, cwd: str = "") -> str | None:
    """Why this tool call must not proceed, or None to allow it.

    The single entry point a PreToolUse hook needs. Pure: no I/O beyond
    resolving paths, so it cannot itself become a source of session failures.
    """
    if tool_name == "Bash":
        return bash_denial_reason(str(tool_input.get("command") or ""))
    if tool_name in ("Read", "Write", "Edit", "NotebookEdit"):
        path_str = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        return file_denial_reason(tool_name, path_str, cwd or os.getcwd())
    return None


def summarize() -> str:
    """One-line description of the active policy, for the run log."""
    hosts = sorted(_egress_allow_hosts())
    egress = ", ".join(hosts) if hosts else "github.com only"
    return (
        f"tool policy: {len(DENIED_TOOLS)} deny rules + PreToolUse guard; "
        f"base={base_branch()}; egress={egress}"
    )


__all__ = [
    "DENIED_TOOLS",
    "bash_denial_reason",
    "denial_reason",
    "file_denial_reason",
    "summarize",
]
