from __future__ import annotations

import typer

from agentcli import (
    credential,
    doctor,
    github,
    install,
    issue_enable,
    labpass,
    pull,
    repos,
    rules,
    rulesets,
    skills,
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
issue_app = typer.Typer(
    name="issue", help="Enable the interactive issue agent on a repo.", no_args_is_help=True
)
skills_app = typer.Typer(
    name="skills", help="Install the workspace's shared Claude skills.", no_args_is_help=True
)
rules_app = typer.Typer(
    name="rules", help="Install the workspace's always-on Claude rules.", no_args_is_help=True
)
setup_app = typer.Typer(
    name="setup",
    help="Human-only privileged setup. Refuses to run with agent credentials.",
    no_args_is_help=True,
)

app.add_typer(github_app)
app.add_typer(workspace_app)
app.add_typer(issue_app)
app.add_typer(skills_app)
app.add_typer(rules_app)
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
def github_token(
    refresh: bool = typer.Option(False, "--refresh", "-f"),
    repo: str = typer.Option(
        None, "--repo", help="scope the token to just this owner/repo (never cached)"
    ),
) -> None:
    """Print a short-lived installation token. Only the token reaches stdout.

    With --repo, the token is narrowed to that single repo (used by the hub so a
    run cannot reach any other installed repo).
    """
    repositories = [repo.rsplit("/", 1)[-1]] if repo else None
    print(github.token(force=refresh, repositories=repositories))


# Dry-run by default (like `setup rulesets`): --apply is the one that writes. The
# labels + caller workflows are the App's own job, so this is NOT human-only.
@issue_app.command("enable")
def issue_enable_command(
    repo: str = typer.Argument(..., help="owner/repo to enable the agent on"),
    ref: str = typer.Option(
        "main", "--ref", help="git ref of the reusable-workflow repo to pin the callers at"
    ),
    reusable_repo: str = typer.Option(
        None,
        "--reusable-repo",
        help="owner/repo hosting the reusable workflows (default: $AGENT_REUSABLE_REPO or brujoand/agent)",
    ),
    apply: bool = typer.Option(False, "--apply", help="Create labels. Without it, only plan."),
    open_pr: bool = typer.Option(
        False, "--open-pr", help="With --apply, open a PR adding the callers instead of printing."
    ),
) -> None:
    """Create the agent labels and lay down the caller workflows on a repo.

    Prints a human-only checklist for the steps the App cannot do (Actions
    secret, reusable-workflow access, runners, branch protection).
    """
    try:
        raise typer.Exit(
            issue_enable.run(
                repo, ref=ref, reusable_repo=reusable_repo, apply=apply, open_pr=open_pr
            )
        )
    except AgentError as err:
        print(f"ERROR: {err}")
        raise typer.Exit(1) from err


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


@skills_app.command("install")
def skills_install() -> None:
    """Symlink the shared skills into ~/.claude/skills/. Idempotent; safe to re-run.

    The links point at the agent checkout, so `agent pull` keeps them current with
    no reinstall. Start a new Claude session to pick up newly linked skills.
    """
    try:
        results = skills.install()
    except AgentError as err:
        print(f"ERROR: {err}")
        raise typer.Exit(1) from err
    for name, outcome in results:
        print(f"  {name:<28} {outcome}")
    print(f"\nskills: {len(results)} shared skill(s) -> {skills.dest_dir()}")


@skills_app.command("list")
def skills_list() -> None:
    """List the shared skills and whether each is linked for this user."""
    available = skills.available()
    if not available:
        print(f"no shared skills at {skills.source_dir()} -- run `agent pull` first")
        return
    for skill in available:
        print(f"  {skill.name:<28} {skills.status(skill.name)}")


@rules_app.command("install")
def rules_install() -> None:
    """Import the shared rules into ~/.claude/CLAUDE.md. Idempotent; safe to re-run.

    Rules are always-on house style, so they go in user-level memory rather than
    in a skill Claude has to choose to load. The imports point at the agent
    checkout, so `agent pull` keeps them current with no reinstall. Start a new
    Claude session to pick up a changed block.
    """
    try:
        outcome, path = rules.install()
    except AgentError as err:
        print(f"ERROR: {err}")
        raise typer.Exit(1) from err
    for rule in rules.available():
        print(f"  {rule.stem:<28} imported")
    print(f"\nrules: block {outcome} in {path}")


@rules_app.command("list")
def rules_list() -> None:
    """List the shared rules and whether the import block is current for this user."""
    available = rules.available()
    if not available:
        print(f"no shared rules at {rules.source_dir()} -- run `agent pull` first")
        return
    state = rules.status()
    for rule in available:
        print(f"  {rule.stem:<28} {state}")
    print(f"\n{rules.memory_file()}: {state}")
