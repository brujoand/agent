from __future__ import annotations

import typer

from agentcli import (
    credential,
    doctor,
    github,
    hooks,
    install,
    issue_enable,
    labpass,
    pull,
    repos,
    rules,
    rulesets,
    skills,
    ssh,
    usage,
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
hooks_app = typer.Typer(
    name="hooks", help="Install the workspace's shared Claude hooks.", no_args_is_help=True
)
setup_app = typer.Typer(
    name="setup",
    help="Human-only privileged setup. Refuses to run with agent credentials.",
    no_args_is_help=True,
)
access_app = typer.Typer(
    name="access", help="step-ca SSH access (baseline certificates).", no_args_is_help=True
)

app.add_typer(github_app)
app.add_typer(workspace_app)
app.add_typer(issue_app)
app.add_typer(skills_app)
app.add_typer(rules_app)
app.add_typer(hooks_app)
app.add_typer(setup_app)
app.add_typer(access_app)


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


@app.command("usage")
def usage_command(
    days: int = typer.Option(30, "--days", "-d", help="Window to report on."),
    as_json: bool = typer.Option(False, "--json", help="Emit the raw report instead of text."),
) -> None:
    """Report what drives Claude Code token spend: context per turn, sessions, repos.

    Reads the local transcripts, not the OTel counters: the counters say how much
    was spent, this says which turns spent it. Costs are list-price equivalents
    for comparing sessions against each other, not a bill.
    """
    raise typer.Exit(usage.run(days=days, as_json=as_json))


@app.command(
    "ssh",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def ssh_command(ctx: typer.Context, host: str = typer.Argument(..., help="Host to reach.")) -> None:
    """SSH to HOST as brujoand-agent, minting a baseline cert first if needed.

    Extra args after HOST pass through to ssh: `agent ssh chromeheim -- uptime`.
    """
    ssh.ssh(host, ctx.args)


@access_app.command("cert")
def access_cert(
    ttl: str = typer.Option(
        None, help="Cert lifetime, e.g. 45m, 1h. Defaults to the configured TTL."
    ),
) -> None:
    """Mint (or refresh) the baseline SSH certificate."""
    _key, cert = ssh.mint_baseline_cert(ttl)
    print(f"Minted baseline certificate: {cert}")


@access_app.command("status")
def access_status() -> None:
    """Show the current baseline certificate (principals, validity)."""
    print(ssh.describe_cert())


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


@hooks_app.command("install")
def hooks_install() -> None:
    """Link the shared hooks into ~/.claude/hooks/ AND wire them into settings.json.

    Both halves, always: a hook Claude Code's settings do not name never runs, so
    a symlink alone would install a silent no-op. The links point at the agent
    checkout, so `agent pull` keeps them current with no reinstall.
    """
    try:
        results, outcome, path = hooks.install()
    except AgentError as err:
        print(f"ERROR: {err}")
        raise typer.Exit(1) from err
    for name, state in results:
        print(f"  {name:<28} {state}")
    print(f"\nhooks: {len(results)} shared hook(s) -> {hooks.dest_dir()}")
    print(f"       wiring {outcome} in {path}")


@hooks_app.command("list")
def hooks_list() -> None:
    """List the shared hooks, where each is wired, and whether it is linked."""
    available = hooks.available()
    if not available:
        print(f"no shared hooks at {hooks.source_dir()} -- run `agent pull` first")
        return
    events: dict[str, list[str]] = {}
    for event, groups in hooks.declaration().items():
        for group in groups:
            label = f"{event}/{group['matcher']}" if group.get("matcher") else event
            for entry in group.get("hooks", []):
                events.setdefault(entry["script"], []).append(label)
    for script in available:
        wired = ", ".join(events.get(script.name, [])) or "NOT WIRED"
        print(f"  {script.name:<28} {hooks.status(script.name):<10} {wired}")
    print(f"\n{hooks.settings_file()}: wiring {hooks.settings_status()}")


# One command for a fresh host. Each installer is idempotent and independent, so
# this is only ever a convenience -- and it keeps every future distribution tree
# reachable from one place instead of a growing list to remember.
@app.command("install")
def install_command(
    lab: bool = typer.Option(
        False, "--lab", help="Also install the lab CLI (needs the sibling gitops checkout)."
    ),
) -> None:
    """Install everything this repo distributes: skills, rules, and hooks.

    `lab` is opt-in: it provisions a toolchain over the network and needs a
    sibling checkout, so a routine `agent install` should not depend on it.
    """
    failures = 0
    for label, run in (
        ("skills", lambda: f"{len(skills.install())} linked -> {skills.dest_dir()}"),
        ("rules", lambda: f"block {rules.install()[0]} in {rules.memory_file()}"),
        ("hooks", lambda: _install_hooks_summary()),
    ):
        try:
            print(f"  {label:<8} {run()}")
        except AgentError as err:
            print(f"  {label:<8} ERROR: {err}")
            failures += 1

    if lab:
        try:
            install.run()
        except AgentError as err:
            print(f"  lab      ERROR: {err}")
            failures += 1
    else:
        print("\n  lab      skipped -- run `agent install --lab` to install it too")

    print(f"\ninstall: {'all set' if not failures else f'{failures} step(s) failed'}")
    print("Start a new Claude session to pick up newly linked skills, rules, and hooks.")
    raise typer.Exit(1 if failures else 0)


def _install_hooks_summary() -> str:
    results, outcome, path = hooks.install()
    return f"{len(results)} linked -> {hooks.dest_dir()}, wiring {outcome} in {path}"
