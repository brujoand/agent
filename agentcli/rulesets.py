"""Apply branch-protection rulesets across every agent-installed repo.

Responsibility is split: **the agent knows which repos, the human decides and
makes the change.**

The agent knows the fleet -- "where is the App installed" is a fact only it
holds -- so `fleet()` always sources the repo list from the App, asking the
agent user for it when this process has no App credentials. Only that answer
crosses the boundary; the App private key never enters a human's process.

The human decides and writes. The ruleset applied here is what stops
`brujoand-agent[bot]` merging its own PRs, so the agent must not be able to
rewrite or delete it -- a lock whose key sits in the pocket of the thing it
locks is not a lock. Every write therefore goes through the invoking human's
`gh` credential, and `require_human_token` refuses to proceed if the active
identity is a bot.

That guard is defence in depth, not the primary control: the brujoand-agent App
is expected to lack `administration: write`, so GitHub would reject the write
anyway. The guard exists to fail early, loudly, and legibly.

Idempotency (verified against the live API):
  * PUT with identical content is a true no-op -- `updated_at` does not move.
  * POST with an existing ruleset name returns HTTP 422 ("Name must be unique").
So a ruleset is matched by *name*, then created if absent and updated if
present. Naive POST-always would fail on every second run.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agentcli import repos
from agentcli.config import AGENT_USER
from agentcli.errors import AgentAuthError, AgentError, AgentHTTPError

# `ruleset_defs`, not `rulesets`: a data directory sharing this module's name
# would shadow it as a package.
_RULESET_DIR = Path(__file__).parent / "ruleset_defs"


def _gh(*args: str, check: bool = True) -> str:
    """Run `gh` as the invoking human and return stdout."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise AgentHTTPError(
            f"gh {' '.join(args[:2])} failed",
            status_code=result.returncode,
            body=(result.stderr or result.stdout).strip()[:500],
        )
    return result.stdout.strip()


def require_human_token() -> str:
    """Abort unless `gh` is authenticated as a real user. Returns the login.

    The probe is `GET /user`, and the signal is whether it *succeeds*:

      * a user token returns `{"login": ...}`
      * a GitHub App installation token gets 403 "Resource not accessible by
        integration" -- it is not a user, so it has no `/user` to fetch

    Checking `type == "Bot"` does not work: that response never arrives. And `gh`
    prints its error JSON to *stdout*, so a naive "did I get output?" test reads
    the error body as an identity, finds no `type`, and waves the bot through.
    Both of those were real bugs here; this fails closed on anything but a
    parseable login.
    """
    result = subprocess.run(["gh", "api", "user"], capture_output=True, text=True, check=False)
    login = ""
    if result.returncode == 0:
        try:
            login = json.loads(result.stdout).get("login") or ""
        except json.JSONDecodeError:
            login = ""

    if not login:
        detail = (result.stderr or result.stdout).strip()[:200]
        raise AgentAuthError(
            "refusing to run: the active GitHub credential is not a human user.\n"
            "  `agent setup rulesets` rewrites the branch protections that "
            "constrain the agent\n"
            "  itself, so it must run as a human admin -- an App installation "
            "token cannot.\n"
            "  Unset GH_TOKEN/GITHUB_TOKEN and authenticate with `gh auth "
            f"login`.\n  gh: {detail}"
        )
    return login


def load(name: str) -> dict:
    path = _RULESET_DIR / f"{name}.json"
    if not path.is_file():
        raise AgentError(f"no such ruleset definition: {path}")
    return json.loads(path.read_text())


def _find_by_name(slug: str, name: str) -> dict | None:
    """The existing ruleset with this name, or None. Name is the join key --
    ids are opaque and differ per repo."""
    existing = json.loads(_gh("api", f"repos/{slug}/rulesets") or "[]")
    for ruleset in existing:
        if ruleset.get("name") == name:
            return ruleset
    return None


def _covers(current, desired) -> bool:
    """True if `current` already satisfies everything `desired` declares.

    A recursive subset test, not equality. GitHub echoes back defaults we never
    declare -- top-level (`id`, `_links`, `created_at`, `bypass_actors`) and,
    crucially, nested ones like `rules[].parameters.required_reviewers: []`. A
    shallow per-key comparison treats `rules` as one opaque value, so a single
    server-added nested key makes every repo look drifted forever, and --apply
    rewrites the whole fleet on every run. Recurse, and ignore what we do not
    declare.

    Lists compare element-wise and order-sensitively: ruleset `rules` come back
    in the order they were sent, and reordering them is a real change.
    """
    if isinstance(desired, dict):
        if not isinstance(current, dict):
            return False
        return all(_covers(current.get(key), value) for key, value in desired.items())
    if isinstance(desired, list):
        if not isinstance(current, list) or len(current) != len(desired):
            return False
        return all(_covers(c, d) for c, d in zip(current, desired, strict=True))
    return current == desired


def _drifts(current: dict, desired: dict) -> bool:
    """True if the live ruleset does not already satisfy the desired one."""
    return not _covers(current, desired)


def apply_to(slug: str, desired: dict, dry_run: bool = True) -> str:
    """Converge one repo.

    Returns 'created' | 'updated' | 'unchanged', or the 'would ...' form under
    dry_run. Never clears bypass_actors: the desired document omits that key, so
    the API leaves existing actors untouched.
    """
    current = _find_by_name(slug, desired["name"])
    payload = json.dumps(desired)

    if current is None:
        if dry_run:
            return "would create"
        _gh_input("POST", f"repos/{slug}/rulesets", payload)
        return "created"

    # The list endpoint returns a summary; drift lives in rules/conditions, which
    # only the per-ruleset endpoint carries.
    detail = json.loads(_gh("api", f"repos/{slug}/rulesets/{current['id']}"))
    if not _drifts(detail, desired):
        return "unchanged"
    if dry_run:
        return "would update"

    _gh_input("PUT", f"repos/{slug}/rulesets/{current['id']}", payload)
    return "updated"


def _gh_input(method: str, path: str, payload: str) -> str:
    """`gh api --method M path --input -`, feeding payload on stdin."""
    result = subprocess.run(
        ["gh", "api", "--method", method, path, "--input", "-"],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AgentHTTPError(
            f"{method} {path} failed",
            status_code=result.returncode,
            body=(result.stderr or result.stdout).strip()[:500],
        )
    return result.stdout


def _fleet_via_agent_user() -> list[str]:
    """Ask the agent user for its own fleet: `sudo -u <user> agent repos`.

    The strict form of the separation. Only the *answer* -- a list of clone URLs
    -- crosses the boundary; the App private key never enters this process. The
    human decides and writes, the agent merely says where.
    """
    result = subprocess.run(
        ["sudo", "-n", "-u", AGENT_USER, "bash", "-lc", "agent repos"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AgentAuthError(
            f"could not ask `{AGENT_USER}` which repos the App is installed on.\n"
            f"  tried: sudo -n -u {AGENT_USER} agent repos\n"
            f"  {(result.stderr or result.stdout).strip()[:200]}"
        )
    return [line for line in result.stdout.split() if line.startswith("https://")]


def fleet() -> tuple[list[str], str]:
    """The repos the brujoand-agent App is installed on, as (sorted slugs, source).

    Responsibility split: **the agent knows which repos, the human decides and
    makes the change.** Enumeration is the agent's -- "where am I installed" is a
    fact only it holds -- so we take it from the App, either directly (when this
    process already has App creds) or by asking the agent user (when it does
    not, which is the normal case, since this command is human-only).

    There is deliberately **no fallback to `gh repo list`**. That would silently
    substitute "repos you own" for "repos the agent touches" -- here, 62 for 11 --
    and `--apply` would write branch protections onto 51 repos that have nothing
    to do with the agent. Failing is correct; guessing the scope is not.
    """
    try:
        urls = repos.clone_urls()
        source = "agent-installed repos (App token)"
    except AgentError:
        urls = _fleet_via_agent_user()
        source = f"agent-installed repos (via `sudo -u {AGENT_USER} agent repos`)"
    return sorted(repos.slug(url) for url in urls), source
