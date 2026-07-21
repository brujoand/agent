"""Enable the agent on a repo the agent App can reach.

Enabling a repo, in the hub model, means: create the labels + lay down the
per-repo **PR-review** workflow + the pre-commit baseline. ISSUES are handled
centrally — the hub poller in the maintainer's infra scans installed repos for
`agent`-labelled issues and runs the agent against them — so a repo needs NO
issue workflow of its own, just the `agent` label. This runs WITH agent
credentials (creating labels + opening the PR are the App's own job).

Two classes of step it CANNOT do — GitHub gates them behind permissions the App
deliberately lacks — so it prints them as a human checklist instead: Actions
secrets / access (CLAUDE_CODE_OAUTH_TOKEN, and — only if the reusable-workflow
repo is private — granting it Actions access to the consumer) and branch
protection (`agent setup rulesets`).

Dry-run by default (render + plan, no writes); `--apply` creates the labels and
either prints the caller workflows for the human to commit or, with `--open-pr`,
opens a PR adding them.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from agentcli import github, repos
from agentcli.errors import AgentHTTPError, AgentInputError

# The repo hosting the reusable workflows the callers invoke. Overridable so a
# fork points its callers at its own copy; defaults to the upstream.
DEFAULT_REUSABLE_REPO = os.environ.get("AGENT_REUSABLE_REPO", "brujoand/agent")

_TEMPLATE_DIR = Path(__file__).parent / "workflow_templates"

# Labels the RUNTIME depends on (issue_agent/agent.py): `agent` is the opt-in the
# workflows gate on; `agent-waiting` is the pause/handoff signal the wrapper
# toggles. Repo-specific triage labels (size/*, needs-scoping) belong to a repo's
# own playbook, not to the shared runtime, so they are deliberately not here.
LABELS = [
    {
        "name": "agent",
        "color": "5319e7",
        "description": "Hand off to the issue agent (the standing opt-in).",
    },
    {
        "name": "agent-waiting",
        "color": "fbca04",
        "description": "Agent paused mid-session; a human reply resumes it.",
    },
]


def installed_slugs() -> set[str]:
    """owner/repo for every repo the agent App is installed on."""
    return {repos.slug(url) for url in repos.clone_urls()}


def ensure_installed(repo: str) -> None:
    """Fail fast unless the App is installed on ``repo`` — nothing downstream
    (labels, a PR, the minted checkout token) works without it."""
    if repo not in installed_slugs():
        raise AgentInputError(
            f"the agent App is not installed on {repo!r}; "
            "install it there first (a human step), then re-run."
        )


def create_label(repo: str, label: dict) -> str:
    """Create one label. Idempotent: an existing label returns 422 -> 'exists'."""
    resp = github.api_post(f"/repos/{repo}/labels", label)
    if resp.status_code == 201:
        return "created"
    if resp.status_code == 422:  # "already_exists" — the label is already there
        return "exists"
    raise AgentHTTPError(
        f"failed to create label {label['name']!r} on {repo}",
        status_code=resp.status_code,
        body=resp.text[:500],
    )


def caller_workflows(ref: str, reusable_repo: str, bot_login: str) -> dict[str, str]:
    """Every caller-workflow file, rendered with the reusable repo/ref pinned and
    the App login filled in.

    Templates carry literal ``{ref}`` / ``{reusable_repo}`` / ``{bot_login}``
    tokens (not str.format — the YAML is full of ``${{ ... }}`` expressions that
    format() would choke on), so plain replaces are correct and safe.
    """
    out: dict[str, str] = {}
    for tmpl in sorted(_TEMPLATE_DIR.glob("*.yml")):
        rendered = (
            tmpl.read_text()
            .replace("{reusable_repo}", reusable_repo)
            .replace("{ref}", ref)
            .replace("{bot_login}", bot_login)
        )
        out[f".github/workflows/{tmpl.name}"] = rendered
    return out


# The standard baseline the agent adds to every repo it is enabled on: a
# pre-commit config (incl. the internal-infra denylist) + a CI workflow that runs
# it. Template filename -> destination path in the target repo.
_BUNDLE_DIR = Path(__file__).parent / "bundle_templates"
_BUNDLE_DEST = {
    "pre-commit-config.yaml": ".pre-commit-config.yaml",
    "pre-commit-ci.yml": ".github/workflows/pre-commit.yml",
}

# Generic internal-infra patterns the denylist blocks (pygrep / Python regex).
# Deliberately hardcodes NO specific private domain (that would leak it into this
# public repo) — a deployment adds its own via AGENT_DENYLIST_EXTRA.
_DENYLIST_PATTERNS = [
    r"\.svc\.cluster\.local",
    r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
    r"\b192\.168\.\d{1,3}\.\d{1,3}\b",
    r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b",
]


def _denylist_regex() -> str:
    """The alternation the pre-commit denylist hook forbids. AGENT_DENYLIST_EXTRA
    (a `|`-joined regex, e.g. your internal domain) is appended, so a deployment
    catches its own patterns without them living in this public repo."""
    patterns = list(_DENYLIST_PATTERNS)
    extra = os.environ.get("AGENT_DENYLIST_EXTRA", "").strip()
    if extra:
        patterns.append(extra)
    return "(" + "|".join(patterns) + ")"


def bundle_files() -> dict[str, str]:
    """The standard pre-commit + CI bundle, keyed by destination path."""
    denylist = _denylist_regex()
    out: dict[str, str] = {}
    for tmpl_name, dest in _BUNDLE_DEST.items():
        out[dest] = (_BUNDLE_DIR / tmpl_name).read_text().replace("{denylist}", denylist)
    return out


def _default_branch(repo: str) -> str:
    resp = github.api_get(f"/repos/{repo}")
    if resp.status_code != 200:
        raise AgentHTTPError(
            f"could not read {repo}", status_code=resp.status_code, body=resp.text[:500]
        )
    return resp.json()["default_branch"]


def _branch_sha(repo: str, branch: str) -> str:
    resp = github.api_get(f"/repos/{repo}/git/ref/heads/{branch}")
    if resp.status_code != 200:
        raise AgentHTTPError(
            f"could not resolve {branch} on {repo}",
            status_code=resp.status_code,
            body=resp.text[:500],
        )
    return resp.json()["object"]["sha"]


def open_enable_pr(
    repo: str,
    files: dict[str, str],
    reusable_repo: str,
    no_clobber: frozenset[str] = frozenset(),
    branch: str = "agent/enable-issue-agent",
) -> str:
    """Create ``branch`` off the default branch, add the files (keyed by repo
    path) to it, and open a PR. Paths in ``no_clobber`` that already exist are
    left untouched, so the repo's own pre-commit config is never overwritten.
    Returns the PR URL."""
    base = _default_branch(repo)
    base_sha = _branch_sha(repo, base)

    # Create the branch (422 if it already exists from a previous run — reuse it).
    ref_resp = github.api_post(
        f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha}
    )
    if ref_resp.status_code not in (201, 422):
        raise AgentHTTPError(
            f"could not create branch {branch} on {repo}",
            status_code=ref_resp.status_code,
            body=ref_resp.text[:500],
        )

    for path, content in files.items():
        # PUT contents needs the blob sha to update an existing file; on a fresh
        # branch the file is absent, so create-only. Look it up on the branch.
        existing = github.api_get(f"/repos/{repo}/contents/{path}", params={"ref": branch})
        if existing.status_code == 200 and path in no_clobber:
            print(f"  skip {path} (already present; not overwriting)")
            continue
        body = {
            "message": f"ci: add {path} (agent enablement)",
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if existing.status_code == 200:
            body["sha"] = existing.json()["sha"]
        put = github.api_put(f"/repos/{repo}/contents/{path}", body)
        if put.status_code not in (200, 201):
            raise AgentHTTPError(
                f"could not write {path} on {repo}",
                status_code=put.status_code,
                body=put.text[:500],
            )

    pr = github.api_post(
        f"/repos/{repo}/pulls",
        {
            "title": "ci: enable the agent (PR review + pre-commit baseline)",
            "head": branch,
            "base": base,
            "body": (
                "Adds the per-repo PR-review workflow (a thin caller of the reusable "
                f"one in `{reusable_repo}`, run on a stock runner) plus a baseline "
                "pre-commit config with an internal-infra denylist and its CI. "
                "Generated by `agent issue enable`.\n\n"
                "Issues are handled by the central hub — no issue workflow here; the "
                "`agent` label is enough. Before this works end-to-end, complete the "
                "human-only steps the command printed (`CLAUDE_CODE_OAUTH_TOKEN`, "
                "branch protection, and — only if the reusable repo is private — "
                "Actions access)."
            ),
        },
    )
    if pr.status_code != 201:
        raise AgentHTTPError(
            f"could not open the enable PR on {repo}",
            status_code=pr.status_code,
            body=pr.text[:500],
        )
    return pr.json()["html_url"]


def _print_checklist(repo: str, reusable_repo: str) -> None:
    print("HUMAN-ONLY steps (the App cannot do these):")
    steps = [
        f"Provide CLAUDE_CODE_OAUTH_TOKEN to {repo} as an Actions secret "
        "(the only secret PR review needs; it runs on github-hosted ubuntu-latest).",
        f"If {reusable_repo} is PRIVATE, grant its reusable workflows Actions "
        f"access to {repo} (Settings -> Actions -> General -> Access). Public "
        "reusable repos need no grant.",
        f"Apply branch protection: `agent setup rulesets --repo {repo}` "
        "(human-only; the App lacks `administration`).",
        f"For ISSUES: no per-repo setup — just apply the `agent` label. The central "
        f"hub picks it up (confirm the App is installed on {repo}).",
        "Optional: `pre-commit install` locally so the baseline (incl. the "
        "internal-infra denylist) also runs on your machine, not only in CI.",
    ]
    for i, line in enumerate(steps, 1):
        print(f"  {i}. {line}")


def run(
    repo: str,
    ref: str = "main",
    reusable_repo: str | None = None,
    apply: bool = False,
    open_pr: bool = False,
) -> int:
    """Enable (or dry-run) the issue agent on ``repo``. Prints a plan + checklist."""
    ensure_installed(repo)
    reusable_repo = reusable_repo or DEFAULT_REUSABLE_REPO
    # The App's own login, auto-filled into the callers so the runtime can tell its
    # own comments apart from a human's without the consumer configuring anything.
    bot_login = github.app_slug()
    # Caller workflows (always fresh) + the standard bundle (pre-commit + CI +
    # denylist), keyed by repo path. The bundle is no-clobber so it never
    # overwrites a repo's own pre-commit setup.
    bundle = bundle_files()
    files = {**caller_workflows(ref, reusable_repo, bot_login), **bundle}
    no_clobber = frozenset(bundle)

    mode = "applying" if apply else "dry-run (pass --apply to write)"
    print(f"enable issue agent on {repo}: {mode}")
    print(f"reusable workflows pinned at {reusable_repo}@{ref} (App login: {bot_login})\n")

    print("labels:")
    for label in LABELS:
        status = create_label(repo, label) if apply else "would create"
        print(f"  {label['name']:<14} {status}")
    print()

    if apply and open_pr:
        url = open_enable_pr(repo, files, reusable_repo, no_clobber=no_clobber)
        print(f"opened PR adding {len(files)} file(s): {url}\n")
    else:
        verb = "add" if apply else "would add"
        print(f"{verb} these files to {repo} (existing pre-commit config is not overwritten):")
        for path, content in files.items():
            print(f"\n----- {path} -----")
            print(content)
        print()

    _print_checklist(repo, reusable_repo)
    return 0
