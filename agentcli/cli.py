from __future__ import annotations

import typer

from agentcli import (
    credential,
    doctor,
    github,
    install,
    labpass,
    pull,
    repos,
    rulesets,
    workspace,
)
from agentcli.config import DEFAULT_REPO
from agentcli.errors import AgentError

app = typer.Typer(
    name="agent",
    help="Agent CLI: credentials, repos, worktrees, and a lab wrapper.",
    no_args_is_help=True,
)

github_app = typer.Typer(name="github", help="brujoand-agent App tokens", no_args_is_help=True)
workspace_app = typer.Typer(name="workspace", help="Session worktrees", no_args_is_help=True)
setup_app = typer.Typer(
    name="setup",
    help="Human-only privileged setup. Refuses to run with agent credentials.",
    no_args_is_help=True,
)

app.add_typer(github_app)
app.add_typer(workspace_app)
app.add_typer(setup_app)


@app.command("git-credential")
def git_credential(action: str = typer.Argument("get", help="get | store | erase")) -> None:
    """git credential helper (gitcredentials(7)). Wired into every clone by `agent pull`."""
    credential.run(action)


@app.command("repos")
def repos_command() -> None:
    """HTTPS clone URLs of every repo the brujoand-agent App is installed on."""
    for url in repos.clone_urls():
        print(url)


@app.command("pull")
def pull_command() -> None:
    """Clone or fast-forward every reachable repo into the agent root."""
    raise typer.Exit(pull.run())


@app.command("doctor")
def doctor_command() -> None:
    """Check credentials, token, reachable repos, lab, and credential helpers."""
    raise typer.Exit(doctor.run())


@github_app.command("token")
def github_token(refresh: bool = typer.Option(False, "--refresh", "-f")) -> None:
    """Print a short-lived installation token. Only the token reaches stdout."""
    print(github.token(force=refresh))


# Dry-run by default: this rewrites branch protections across the whole fleet, so
# the destructive path is the one you have to ask for.
@setup_app.command("rulesets")
def setup_rulesets(
    apply: bool = typer.Option(False, "--apply", help="Write. Without it, only diff."),
    ruleset: str = typer.Option("protect-main-pr-only", "--ruleset"),
    repo: str = typer.Option("", "--repo", help="One owner/repo instead of the fleet."),
) -> None:
    """Converge branch-protection rulesets across every agent-installed repo.

    Human-only: the ruleset applied here is what prevents brujoand-agent[bot]
    from merging its own PRs, so the agent may not rewrite it.
    """
    try:
        login = rulesets.require_human_token()
        desired = rulesets.load(ruleset)
        targets, source = ([repo], "explicit --repo") if repo else rulesets.fleet()
    except AgentError as err:
        print(f"ERROR: {err}")
        raise typer.Exit(1) from err

    mode = "applying" if apply else "dry-run (pass --apply to write)"
    print(f"{ruleset}: {mode} as {login}")
    print(f"targets: {len(targets)} repo(s) from {source}\n")

    failures = 0
    for slug in targets:
        try:
            outcome = rulesets.apply_to(slug, desired, dry_run=not apply)
        except AgentError as err:
            outcome, failures = f"ERROR: {err}", failures + 1
        print(f"  {slug:<45} {outcome}")

    raise typer.Exit(1 if failures else 0)


# `lab` is one command, not a Typer sub-app: a sub-app would try to resolve
# `agent lab k8s explode` as a subcommand named `k8s` and fail before the
# passthrough ever ran. Everything after `lab` is forwarded verbatim, so lab (not
# Typer) owns the flag grammar -- `agent lab flux sync -n foo` reaches lab intact.
@app.command(
    "lab",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    help="Install lab, or run it with agent credentials: agent lab <args...>",
)
def lab_command(ctx: typer.Context) -> None:
    args = list(ctx.args)
    if not args:
        print("Usage: agent lab install [--repo <name>] | agent lab <args...>")
        raise typer.Exit(0)

    if args[0] == "install":
        repo = DEFAULT_REPO
        rest = args[1:]
        if rest[:1] == ["--repo"] and len(rest) > 1:
            repo = rest[1]
        raise typer.Exit(install.run(repo))

    labpass.exec_lab(args)


@workspace_app.command("create")
def workspace_create(
    branch: str = typer.Argument(..., help="<type>/<slug>, e.g. feat/my-change"),
    repo: str = typer.Option(DEFAULT_REPO, "--repo"),
) -> None:
    """Branch off the repo's freshly-fetched default branch. Prints only its path."""
    print(workspace.create(branch, repo))


@workspace_app.command("delete")
def workspace_delete(
    slug: str = typer.Argument(..., help="<slug> or <type>/<slug>"),
    repo: str = typer.Option(DEFAULT_REPO, "--repo"),
) -> None:
    """Remove a session worktree. Refuses if it has uncommitted work."""
    print(f"removed {workspace.delete(slug, repo)}")


@workspace_app.command("list")
def workspace_list(repo: str = typer.Option(None, "--repo")) -> None:
    """List session worktrees across every managed repo, annotated [in use] / [idle]."""
    for name in [repo] if repo else workspace.managed_repos():
        for worktree in workspace.session_worktrees(name):
            state = "[in use]" if workspace.in_use(worktree) else "[idle]"
            print(f"{worktree}  {state}")


@workspace_app.command("gc")
def workspace_gc(repo: str = typer.Option(None, "--repo")) -> None:
    """Remove idle worktrees untouched for >24h. Never forces; skips dirty ones."""
    removed = workspace.gc(repo)
    print(f"gc: removed {removed} worktree(s)")
