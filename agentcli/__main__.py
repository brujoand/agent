from __future__ import annotations

import sys

from agentcli.cli import app
from agentcli.errors import AgentAuthError, AgentError, AgentHTTPError


def run() -> None:
    """Map typed errors onto exit codes, and never show a traceback for one.

    An agent reads these on stderr; a stack trace buries the actionable line.
    Anything that is not an AgentError is a genuine bug and propagates as usual.
    """
    try:
        app()
    except AgentAuthError as exc:
        print(f"agent: {exc}", file=sys.stderr)
        sys.exit(2)
    except AgentHTTPError as exc:
        print(f"agent: {exc}", file=sys.stderr)
        if exc.body:
            print(exc.body, file=sys.stderr)
        sys.exit(1)
    except AgentError as exc:
        print(f"agent: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()
