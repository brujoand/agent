"""bagent -- the host-side launcher that runs an agent in a container.

The other half of `agent`, on the other side of the container boundary.

`agent` is the agent's own CLI: it holds App credentials, clones repos, mints
tokens, and runs INSIDE the container as the agent user. `bagent` is installed
by a HUMAN, on their own account, and holds nothing. Its whole job is to answer
"which repo did you mean" and then hand that answer to `docker run`.

That split is the point. It replaces su'ing to a dedicated unix user: instead of
one account on the box with the agent's credentials and a shared filesystem, the
agent gets a container that can see exactly two paths::

    /mnt/src        the checkouts, shared with the human (read-only in practice)
    /mnt/bagent     the agent's HOME -- credentials, ~/.claude, caches

Nothing else is mounted, so the agent cannot reach the human's home, SSH keys,
kubeconfig, or anything else on the host unless it is asked for explicitly (see
PROFILES). And because HOME is a mount rather than an image layer, the agent's
state persists across runs while the environment itself is whatever the image
currently is -- rebuild the image and the next session is up to date.

**The boundary is one-directional, and it is worth being honest about which.**
It stops the AGENT reaching the human's files. It does not stop the human
reading `/mnt/bagent`: anyone who can talk to the Docker daemon is effectively
root on the box. That is fine -- it is the human's machine and the human
provisioned those credentials -- but this is containment of the agent, not a
secret store.

Deployment-neutral on purpose: every path, image and user name below comes from
the environment, so this file names no host and no deployment. This repo is
public.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import typer

# --- what the launcher is pointed at ----------------------------------------
#
# All overridable, none derived from this file's location: the installed copy
# lives in the human's own tool directory, which says nothing about where the
# checkouts are.

DEFAULT_SRC = "/mnt/src"
DEFAULT_HOME = "/mnt/bagent"
DEFAULT_IMAGE = "ghcr.io/brujoand/brujoand-agent:latest"
# How long an image may go unchecked before the next launch refreshes it. The
# "always up to date" property is only true if something actually pulls.
PULL_TTL_SECONDS = 24 * 60 * 60


def src_root() -> Path:
    return Path(os.environ.get("BAGENT_SRC", DEFAULT_SRC))


def home_mount() -> Path:
    return Path(os.environ.get("BAGENT_HOME", DEFAULT_HOME))


def image() -> str:
    return os.environ.get("BAGENT_IMAGE", DEFAULT_IMAGE)


def pull_stamp() -> Path:
    """Where the last image check is recorded -- in the HUMAN's cache, not the mount."""
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "bagent" / "last-pull"


# --- elevated access --------------------------------------------------------
#
# The default container has no credentials of the human's at all. A profile
# lends it one, for one invocation, named at the command line -- so reaching the
# cluster is a thing you type, not a thing that is quietly always true.
#
# Sources are standard tool paths, never deployment specifics: what is inside a
# kubeconfig is the deployment's business, and none of it is written here.
# `bagent --with kube` says "lend it my kubeconfig", not which cluster that is.


@dataclass(frozen=True)
class Profile:
    name: str
    source: str  # host path, `~` allowed
    target: str  # path inside the container, relative to HOME when not absolute
    why: str


PROFILES: dict[str, Profile] = {
    "kube": Profile("kube", "~/.kube", ".kube", "kubectl against the human's clusters"),
    "talos": Profile("talos", "~/.talos", ".talos", "talosctl against the human's nodes"),
    # Deliberately NOT ~/.ssh: mounting the whole directory would also hand over
    # known_hosts and every other key. One key, and it lands beside the agent's
    # own ssh dir rather than on top of it.
    "ssh": Profile("ssh", "~/.ssh/id_ed25519", ".ssh-host/id_ed25519", "SSH as the human"),
}


def profile_mount(profile: Profile, home: Path) -> str:
    """The `-v` argument lending one credential to the container, read-only."""
    source = Path(profile.source).expanduser()
    target = profile.target
    dest = target if target.startswith("/") else str(home / target)
    return f"{source}:{dest}:ro"


# --- finding the repo -------------------------------------------------------


@dataclass(frozen=True)
class Repo:
    owner: str
    name: str
    path: Path

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


def discover(root: Path | None = None) -> list[Repo]:
    """Every checkout under <root>/<owner>/<repo>, sorted.

    A directory only counts as a repo if it carries `.git` -- `/mnt/src` is a
    shared mount and may hold anything else the human put there.
    """
    root = root if root is not None else src_root()
    found: list[Repo] = []
    try:
        owners = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    for owner in owners:
        try:
            entries = sorted(p for p in owner.iterdir() if p.is_dir())
        except OSError:
            continue
        for entry in entries:
            if (entry / ".git").exists():
                found.append(Repo(owner=owner.name, name=entry.name, path=entry))
    return found


def _by_name(repos: list[Repo]) -> dict[str, list[Repo]]:
    grouped: dict[str, list[Repo]] = {}
    for repo in repos:
        grouped.setdefault(repo.name, []).append(repo)
    return grouped


def tokens(repos: list[Repo]) -> list[str]:
    """What a human may type, one per repo.

    A bare name while it is unique, and `owner/name` once it is not -- so the
    short form keeps working for everything except the collisions, and the
    moment a second `owner/infra` appears BOTH become owner-qualified rather
    than one of them silently keeping the short name.
    """
    grouped = _by_name(repos)
    out: list[str] = []
    for repo in repos:
        out.append(repo.name if len(grouped[repo.name]) == 1 else repo.slug)
    return sorted(out)


def resolve(token: str, repos: list[Repo]) -> Repo:
    """The repo a token names. Raises typer.BadParameter with a usable message."""
    if not token:
        raise typer.BadParameter("no repo given")

    if "/" in token:
        owner, _, name = token.partition("/")
        for repo in repos:
            if repo.owner == owner and repo.name == name:
                return repo
        raise typer.BadParameter(f"no checkout at {src_root()}/{owner}/{name}")

    same_name = _by_name(repos).get(token, [])
    if len(same_name) == 1:
        return same_name[0]
    if not same_name:
        raise typer.BadParameter(
            f"no repo named {token!r} under {src_root()} -- `bagent --list` shows what is there"
        )
    owners = ", ".join(sorted(m.slug for m in same_name))
    raise typer.BadParameter(f"{token!r} is ambiguous; name the owner: {owners}")


def matches(incomplete: str, candidates: list[str]) -> list[str]:
    """Candidates worth offering for a partial word.

    Matches the whole token OR the repo-name half of an owner-qualified one, so
    typing the repo name still reaches a collision. Without that second rule,
    `infra<TAB>` offers nothing at all the moment a second `infra` appears --
    the name is exactly what a human types, and it is the only part they know.
    """
    return [
        c
        for c in candidates
        if c.startswith(incomplete) or c.partition("/")[2].startswith(incomplete)
    ]


def complete_repo(incomplete: str) -> list[str]:
    """Shell completion. Never raises -- a broken completer breaks the shell."""
    try:
        return matches(incomplete, tokens(discover()))
    except Exception:  # pragma: no cover - defensive
        return []


# --- building the run -------------------------------------------------------


def docker_command(
    repo: Repo,
    *,
    command: list[str] | None = None,
    profiles: list[str] | None = None,
    mounts: list[str] | None = None,
    user: str | None = None,
    src: Path | None = None,
    home: Path | None = None,
    img: str | None = None,
) -> list[str]:
    """The full `docker run` argv. Pure, so the interesting part is testable.

    Only two mounts by default. Everything else is opt-in and read-only.
    """
    src = src if src is not None else src_root()
    home = home if home is not None else home_mount()
    img = img if img is not None else image()

    argv = [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--tty",
        "--volume",
        f"{src}:{src}",
        "--volume",
        f"{home}:{home}",
        # HOME is the mount, so ~/.claude, ~/.cache/agent and the App creds
        # persist across runs while the image stays disposable.
        "--env",
        f"HOME={home}",
        # The agent's own CLI resolves checkouts from here.
        "--env",
        f"AGENT_SRC_ROOT={src}",
        "--workdir",
        str(repo.path),
    ]

    for name in profiles or []:
        profile = PROFILES.get(name)
        if profile is None:
            known = ", ".join(sorted(PROFILES))
            raise typer.BadParameter(f"unknown profile {name!r}; known: {known}")
        argv += ["--volume", profile_mount(profile, home)]

    for spec in mounts or []:
        argv += ["--volume", spec]

    if user:
        argv += ["--user", user]

    argv.append(img)
    argv += command or []
    return argv


def should_pull(stamp: Path, now: float, ttl: int = PULL_TTL_SECONDS) -> bool:
    """Has the image gone unchecked for longer than the TTL?

    Never pulled -> yes. Unreadable stamp -> yes: erring toward one extra pull
    is cheaper than silently running a stale image for weeks.
    """
    try:
        return (now - stamp.stat().st_mtime) > ttl
    except OSError:
        return True


def _touch(stamp: Path) -> None:
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass  # a cache we cannot write is not a reason to fail the launch


def pull_image(img: str, stamp: Path) -> None:
    """Refresh the image, recording the attempt even if it fails.

    A failed pull is a warning, not an error: being offline must not stop a
    session from starting on the image already on disk.
    """
    print(f"bagent: refreshing {img}", file=sys.stderr)
    result = subprocess.run(["docker", "pull", img], check=False)
    if result.returncode != 0:
        print("bagent: pull failed; using the image already on disk", file=sys.stderr)
    _touch(stamp)


# --- the CLI ----------------------------------------------------------------

app = typer.Typer(
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _list_repos() -> None:
    repos = discover()
    if not repos:
        print(f"no checkouts under {src_root()}")
        return
    grouped = _by_name(repos)
    for repo in repos:
        ambiguous = len(grouped[repo.name]) > 1
        token = repo.slug if ambiguous else repo.name
        note = "  (name collides -- owner required)" if ambiguous else ""
        print(f"  {token:<40} {repo.path}{note}")


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def main(
    ctx: typer.Context,
    repo: str = typer.Argument(
        "",
        autocompletion=complete_repo,
        help="Repo to work in: a bare name, or owner/name when the name collides.",
    ),
    shell: bool = typer.Option(False, "--shell", help="Start a shell instead of Claude Code."),
    with_: list[str] = typer.Option(
        [],
        "--with",
        "-w",
        help="Lend the container one of your credentials, read-only (kube, talos, ssh).",
    ),
    mount: list[str] = typer.Option(
        [], "--mount", "-m", help="Ad-hoc extra mount, `SRC:DST[:ro]`. Repeatable."
    ),
    user: str = typer.Option("", "--user", "-u", help="Override the container user."),
    pull: bool = typer.Option(False, "--pull", help="Refresh the image before starting."),
    no_pull: bool = typer.Option(False, "--no-pull", help="Never refresh, even if stale."),
    list_repos: bool = typer.Option(False, "--list", "-l", help="List what can be launched."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the docker command instead of running it."
    ),
) -> None:
    """Run an agent in a container, in one of your checkouts.

    The container sees two paths and nothing else: the checkouts and the agent's
    own home. Anything of yours it needs beyond that is lent explicitly with
    `--with`, for that one session.
    """
    if list_repos:
        _list_repos()
        return

    if not repo:
        print("bagent: name a repo (`bagent --list` shows what is there)", file=sys.stderr)
        raise typer.Exit(2)

    if shutil.which("docker") is None:
        print("bagent: docker is not on PATH", file=sys.stderr)
        raise typer.Exit(1)

    root = src_root()
    if not root.is_dir():
        print(f"bagent: {root} does not exist -- nothing to launch", file=sys.stderr)
        raise typer.Exit(1)

    target = resolve(repo, discover(root))

    # Trailing args after the repo are the command to run, so `bagent x -- ls -l`
    # and `bagent x ls -l` both work. --shell is the common case, spelled short.
    extra = list(ctx.args)
    command = extra or (["bash"] if shell else ["claude"])

    argv = docker_command(
        target,
        command=command,
        profiles=with_,
        mounts=mount,
        user=user or None,
        src=root,
    )

    if dry_run:
        print(" ".join(argv))
        return

    if pull or (not no_pull and should_pull(pull_stamp(), time.time())):
        pull_image(image(), pull_stamp())

    for name in with_:
        print(f"bagent: lending {PROFILES[name].source} ({PROFILES[name].why})", file=sys.stderr)

    raise typer.Exit(subprocess.run(argv, check=False).returncode)


def run() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    run()
