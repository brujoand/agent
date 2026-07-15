"""Enable the interactive issue agent on a repo the brujoand-agent App can reach.

Unlike `agent setup rulesets` (human-only — it rewrites the very lock that stops
the agent merging its own PRs), enabling is the agent's *own* job: create the
labels the runtime depends on, and lay down thin caller workflows that invoke the
shared reusable workflows in brujoand/agent. So this runs WITH agent credentials.

Two classes of step it CANNOT do — GitHub gates them behind permissions the
brujoand-agent App deliberately lacks — so it prints them as a human checklist
instead: Actions secrets / org settings (CLAUDE_CODE_OAUTH_TOKEN, granting the
private agent repo's reusable workflows access to the consumer, the runner group)
and branch protection (`agent setup rulesets`).

Dry-run by default (render + plan, no writes); `--apply` creates the labels and
either prints the caller workflows for the human to commit or, with `--open-pr`,
opens a PR adding them.
"""

from __future__ import annotations

import base64
from pathlib import Path

from agentcli import github, repos
from agentcli.errors import AgentHTTPError, AgentInputError

# The repo that hosts the reusable workflows the callers invoke.
REUSABLE_REPO = "brujoand/agent"

_TEMPLATE_DIR = Path(__file__).parent / "workflow_templates"

# Labels the RUNTIME depends on (issue_agent/agent.py): `agent` is the opt-in the
# workflows gate on; `agent-waiting` is the pause/handoff signal the wrapper
# toggles. Repo-specific triage labels (size/*, needs-scoping) belong to a repo's
# own playbook, not to the shared runtime, so they are deliberately not here.
LABELS = [
    {
        "name": "agent",
        "color": "5319e7",
        "description": "Hand off to the Claude issue agent (the standing opt-in).",
    },
    {
        "name": "agent-waiting",
        "color": "fbca04",
        "description": "Agent paused mid-session; a human reply resumes it.",
    },
]


def installed_slugs() -> set[str]:
    """owner/repo for every repo the brujoand-agent App is installed on."""
    return {repos.slug(url) for url in repos.clone_urls()}


def ensure_installed(repo: str) -> None:
    """Fail fast unless the App is installed on ``repo`` — nothing downstream
    (labels, a PR, the minted checkout token) works without it."""
    if repo not in installed_slugs():
        raise AgentInputError(
            f"the brujoand-agent App is not installed on {repo!r}; "
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


def caller_workflows(ref: str) -> dict[str, str]:
    """Every caller-workflow file, rendered with the reusable ref pinned.

    Templates carry a literal ``{ref}`` token (not str.format — the YAML is full
    of ``${{ ... }}`` expressions that format() would choke on), so a plain
    replace is both correct and safe.
    """
    return {
        tmpl.name: tmpl.read_text().replace("{ref}", ref)
        for tmpl in sorted(_TEMPLATE_DIR.glob("*.yml"))
    }


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
    repo: str, files: dict[str, str], branch: str = "agent/enable-issue-agent"
) -> str:
    """Create ``branch`` off the default branch, add the caller workflows to it,
    and open a PR. Returns the PR URL."""
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

    for name, content in files.items():
        path = f".github/workflows/{name}"
        # PUT contents needs the blob sha to update an existing file; on a fresh
        # branch the file is absent, so create-only. Look it up on the branch.
        existing = github.api_get(f"/repos/{repo}/contents/{path}", params={"ref": branch})
        body = {
            "message": f"ci: add {name} caller for the issue agent",
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
            "title": "ci: enable the Claude issue agent",
            "head": branch,
            "base": base,
            "body": (
                "Adds the thin caller workflows that invoke the shared reusable "
                f"workflows in `{REUSABLE_REPO}`. Generated by `agent issue enable`.\n\n"
                "Before this works end-to-end, complete the human-only steps the "
                "command printed (reusable-workflow access, `CLAUDE_CODE_OAUTH_TOKEN`, "
                "runner group, branch protection)."
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


def _print_checklist(repo: str) -> None:
    print("HUMAN-ONLY steps (the brujoand-agent App cannot do these):")
    steps = [
        f"Grant {REUSABLE_REPO}'s reusable workflows access to {repo} "
        "(Settings -> Actions -> General -> Access, or org-wide). Without this, "
        "callers fail with 'workflow was not found'.",
        f"Provide CLAUDE_CODE_OAUTH_TOKEN to {repo} as an org or repo Actions secret.",
        f"Add {repo} to the org runner group so `homelab-runners` serves it.",
        f"Apply branch protection: `agent setup rulesets --repo {repo}` "
        "(human-only; the App lacks `administration`).",
    ]
    for i, line in enumerate(steps, 1):
        print(f"  {i}. {line}")


def run(repo: str, ref: str = "main", apply: bool = False, open_pr: bool = False) -> int:
    """Enable (or dry-run) the issue agent on ``repo``. Prints a plan + checklist."""
    ensure_installed(repo)
    files = caller_workflows(ref)

    mode = "applying" if apply else "dry-run (pass --apply to write)"
    print(f"enable issue agent on {repo}: {mode}")
    print(f"reusable workflows pinned at {REUSABLE_REPO}@{ref}\n")

    print("labels:")
    for label in LABELS:
        status = create_label(repo, label) if apply else "would create"
        print(f"  {label['name']:<14} {status}")
    print()

    if apply and open_pr:
        url = open_enable_pr(repo, files)
        print(f"opened PR adding {len(files)} caller workflow(s): {url}\n")
    else:
        verb = "add" if apply else "would add"
        print(f"{verb} these caller workflows under {repo} .github/workflows/ :")
        for name, content in files.items():
            print(f"\n----- .github/workflows/{name} -----")
            print(content)
        print()

    _print_checklist(repo)
    return 0
