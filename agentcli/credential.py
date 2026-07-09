from __future__ import annotations

import sys

from agentcli import github


def run(action: str, stdin=None, stdout=None) -> None:
    """git credential helper (see gitcredentials(7)).

    Only `get` does anything. The token is short-lived and already cached, so
    `store` and `erase` are no-ops -- there is nothing to persist or wipe.

    git writes a `key=value` request terminated by a blank line. Drain it even
    when we are about to ignore it: if we reply and exit while git is still
    writing, git takes SIGPIPE. The attributes carry no information we need --
    the token is host-scoped and the username for an App token is always the
    literal `x-access-token`.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    for line in stdin:
        if not line.strip():
            break

    if action != "get":
        return

    stdout.write(f"username=x-access-token\npassword={github.token()}\n")
