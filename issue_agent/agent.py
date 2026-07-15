#!/usr/bin/env python3
"""Interactive GitHub issue agent.

Holds ONE live agent session for the whole conversation with a GitHub issue.
The LLM backend sits behind the provider abstraction in ``providers/``
(``AGENT_PROVIDER``, default ``claude`` = the Claude Agent SDK adapter); this
wrapper never touches a provider SDK directly. The agent investigates, and
either asks the user clarifying questions (posted as issue comments) or acts
(opens a ready PR / posts findings). When it asks, this wrapper blocks polling
the issue thread for the user's reply and feeds it back into the SAME session
— no re-ingest of context per turn.

The Claude provider mirrors the session transcript to MinIO (S3-compatible)
via ``S3SessionStore``, keyed by a deterministic session id derived from the
issue. If the job is killed by the GitHub Actions timeout mid-conversation, a
later comment on the `agent`-labelled thread re-runs this wrapper which
RESUMES the same session from MinIO and continues synchronously. Providers
without persistence always start fresh.

Flow control is model-driven via sentinel markers the agent emits in its text:
  - ``<<<ASK>>>`` ... ``<<<END_ASK>>>``   -> post the enclosed text as a question,
                                            append a "live, waiting for reply"
                                            note (+ runner pod), and block for the
                                            user's reply.
  - ``<<<DONE>>>`` ... ``<<<END_DONE>>>`` -> the agent finished; post the enclosed
                                            reader-facing summary (PR link + a line
                                            or two, or L/XL findings) as ONE comment
                                            with the "session ended, not waiting"
                                            footer appended, then exit 0. A bare
                                            ``<<<DONE>>>`` (no enclosed summary) is
                                            still honoured as "finished" and yields
                                            a footer-only comment. The model does
                                            NOT post its own summary comment — the
                                            wrapper owns the single closing comment.

Every wrapper-posted status note (announcement / waiting / paused / ended) carries
the runner context from ``runner_context()`` (via ``with_runner_context()``) so a
human can find the live pod / run log, and is emitted so the human never has to
guess whether the agent is still standing by.

Environment:
  ISSUE_NUMBER         (required) the issue to work
  GITHUB_REPOSITORY    (required) owner/repo (provided by Actions)
  GITHUB_TOKEN         (required) for gh CLI (provided by Actions)
  AGENT_PROVIDER       (optional) default claude; selects the providers/ adapter
  AGENT_MODEL          (optional) default claude-opus-4-8; passed through to the
                       provider opaquely
  MAX_RUNTIME_SECONDS  (optional) default 2940 (49 min); exit cleanly before the
                       GitHub job timeout-minutes kills the pod.
  POLL_INTERVAL_SECONDS(optional) default 20
  TRIGGER_SOURCE       (optional) default "human"; provenance stamped on the
                       run-record (terminal comment + job summary). Autopilot
                       workflows set e.g. "autopilot-schedule" so an unattended
                       run is distinguishable from a human-labelled one.
  AGENT_BASE_BRANCH    (optional) PR base branch; default = the target repo's
                       default branch (queried via gh), else main
  AGENT_PLAYBOOK       (optional) repo-relative playbook path; default
                       .claude/commands/triage-and-fix.md, falling back to the
                       generic default-playbook.md shipped beside this wrapper

Claude provider only (AGENT_PROVIDER=claude):
  CLAUDE_CODE_OAUTH_TOKEN (required) consumed by the Claude Code CLI the SDK runs
  MINIO_ENDPOINT_URL   (required) e.g. http://minio.data.svc.cluster.local:80
  MINIO_BUCKET         (required) e.g. issue-agent-sessions
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (required) bucket-scoped MinIO creds
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import anyio
from providers import SessionConfig, TurnUsage, create_provider

# Stable namespace so the session id for an issue is reproducible across runs.
_SESSION_NAMESPACE = uuid.UUID("6f3b2c1a-9d4e-5a6b-8c7d-0e1f2a3b4c5d")

ASK_RE = re.compile(r"<<<ASK>>>(.*?)<<<END_ASK>>>", re.DOTALL)
DONE_MARKER = "<<<DONE>>>"
# Paired DONE marker: the enclosed text is the reader-facing summary the wrapper
# posts as the single closing comment. Bare DONE_MARKER (no <<<END_DONE>>>) is
# still detected as "finished" — DONE_RE just misses and we fall back to a
# footer-only comment. DONE_MARKER is a substring of the paired form, so the same
# `DONE_MARKER in text` check triggers on both old- and new-style emissions.
DONE_RE = re.compile(r"<<<DONE>>>(.*?)<<<END_DONE>>>", re.DOTALL)

# The agent's own GitHub identity. EVERY comment the agent posts — the wrapper's
# status notes AND the model's own `gh issue comment` calls — is authored by the
# brujoand-agent App, because the whole job runs on its installation token. That
# single author is how we tell agent output apart from a human's:
#   - here in the wrapper, `gh ... --json comments` (GraphQL) returns the App's
#     BARE login `brujoand-agent` (NO `[bot]` suffix), so latest_human_comment()
#     and issue_already_links_pr() match on exactly this string;
#   - in the resume/pr-agent workflow guards, the webhook payload instead reports
#     `brujoand-agent[bot]` with `author_association == NONE`, so their existing
#     positive-association requirement (OWNER/MEMBER/COLLABORATOR or `brujoand`)
#     already excludes the agent — no body inspection needed.
# This replaced an in-body `<!-- issue-agent -->` marker that was only necessary
# during the PAT era, when agent comments were indistinguishable from the
# maintainer's. The App gives us a stable author again, and unlike the marker it
# also covers the model's own comments (which never carried the marker).
AGENT_BOT_LOGIN = "brujoand-agent"

# Leading line of the one-time startup banner (post_announcement). It doubles as
# the idempotency key: a comment authored by AGENT_BOT_LOGIN containing this
# exact string means the banner is already up, so the opener posts it once and
# every later resume skips it. Same author+content idiom as issue_already_links_pr
# — no in-body marker (see AGENT_BOT_LOGIN note).
ANNOUNCE_LEAD = ":robot: **Claude agent is on it.**"


def env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        print(f"FATAL: missing required env {name}", file=sys.stderr)
        sys.exit(2)
    return val or ""


def gh(*args: str, check: bool = True) -> str:
    """Run a gh CLI command and return stdout."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        print(f"gh {' '.join(args)} failed: {result.stderr}", file=sys.stderr)
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def post_comment(view_cmd: str, issue: str, repo: str, body: str) -> None:
    """Post a comment on the issue/PR as the brujoand-agent App.

    The App identity (AGENT_BOT_LOGIN) is what the workflow guards and
    latest_human_comment() use to exclude agent output, so no in-body marker is
    needed; the body is posted verbatim.
    """
    gh(
        view_cmd,
        "comment",
        issue,
        "--repo",
        repo,
        "--body",
        body,
        check=False,
    )


def session_id_for(repo: str, issue: str) -> str:
    return str(uuid.uuid5(_SESSION_NAMESPACE, f"{repo}#{issue}"))


def pr_session_id_for(repo: str, pr: str) -> str:
    # Distinct namespace ("!pr#") so a PR session never collides with the
    # issue session that spawned it.
    return str(uuid.uuid5(_SESSION_NAMESPACE, f"{repo}!pr#{pr}"))


def latest_human_comment(repo: str, issue: str, since_iso: str) -> str | None:
    """Return the body of the newest human comment after ``since_iso``, else None.

    A "human" comment is one NOT authored by the agent App. Every agent comment
    — wrapper status notes and the model's own `gh` comments alike — is authored
    by AGENT_BOT_LOGIN (``gh`` reports the bare login, no ``[bot]`` suffix), so
    excluding that one login guarantees the agent never consumes its own
    ASK/pause comments as a "reply". Other bots (``github-actions`` etc.) are
    excluded too, since a bot comment is never the human reply we are polling for.
    """
    raw = gh(
        "issue",
        "view",
        issue,
        "--repo",
        repo,
        "--json",
        "comments",
    )
    comments = json.loads(raw).get("comments", [])
    newest: tuple[str, str] | None = None
    for c in comments:
        author = (c.get("author") or {}).get("login", "")
        is_agent = author == AGENT_BOT_LOGIN
        is_bot = author.endswith("[bot]") or author == "github-actions"
        body = c.get("body", "")
        created = c.get("createdAt", "")
        if is_agent or is_bot or not created or created <= since_iso:
            continue
        if newest is None or created > newest[0]:
            newest = (created, body)
    return newest[1] if newest else None


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def runner_context() -> str:
    """Human-readable pointer to WHERE this session is running: the ephemeral
    runner pod and a link to the Actions run log. Appended to the wrapper's
    status comments (waiting / paused / ended) so a maintainer can find the live
    pod or its logs. Returns "" when neither is discoverable (e.g. run outside
    Actions), so callers can append conditionally. In an ARC runner pod the
    container HOSTNAME is the pod name; RUNNER_NAME is the fallback."""
    pod = os.environ.get("HOSTNAME") or os.environ.get("RUNNER_NAME") or ""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    parts: list[str] = []
    if pod:
        parts.append(f"pod `{pod}`")
    if repo and run_id:
        parts.append(f"[run log]({server}/{repo}/actions/runs/{run_id})")
    return " · ".join(parts)


def with_runner_context(note: str, verb: str = "Running in") -> str:
    """Append the ``runner_context()`` pointer to a status note, or return it
    unchanged when nothing is discoverable. Single place for the format so the
    announcement / waiting / paused / ended notes stay consistent — ``verb``
    tenses it per note (e.g. "Running in" vs "Was running in" vs "Ran in")."""
    where = runner_context()
    return f"{note}\n\n{verb} {where}." if where else note


def run_record(status: str) -> str:
    """A compact, greppable provenance line for a terminal run, appended to the
    ended/paused comment and the job summary so an unattended run is auditable
    from the thread. ``status`` is the run's outcome (completed / paused).
    ``TRIGGER_SOURCE`` (default ``human``) is stamped by the invoking workflow —
    an autopilot names its trigger (e.g. ``autopilot-schedule``) so a reader can
    tell an unprompted run from a human-labelled one. The run URL/pod already
    ride the runner-context footer, so this adds only the source+status it omits."""
    source = env("TRIGGER_SOURCE", "human")
    return f":card_index_dividers: **Run {status}** · trigger `{source}`"


def already_announced(view_cmd: str, repo: str, issue: str) -> bool:
    """True if the startup banner (ANNOUNCE_LEAD) is already on the issue/PR.

    Idempotency guard: keyed on author (AGENT_BOT_LOGIN) + the banner's lead line
    so the opener posts it once and every resume skips it. `gh` GraphQL reports
    the bare App login, matching AGENT_BOT_LOGIN (see that note).
    """
    raw = gh(view_cmd, "view", issue, "--repo", repo, "--json", "comments", check=False)
    if not raw:
        return False
    try:
        comments = json.loads(raw).get("comments", [])
    except json.JSONDecodeError:
        return False
    for c in comments:
        author = (c.get("author") or {}).get("login", "")
        if author == AGENT_BOT_LOGIN and ANNOUNCE_LEAD in c.get("body", ""):
            return True
    return False


def post_announcement(view_cmd: str, repo: str, issue: str) -> None:
    """Post the one-time startup banner announcing the agent is active here.

    Tells a human, without reading Actions logs, that an agent has picked up the
    thread, where it is running, and that no mention is needed to talk to it.
    Idempotent via already_announced() so the opener posts it once and resumes
    don't repeat it.
    """
    if already_announced(view_cmd, repo, issue):
        return
    body = (
        f"{ANNOUNCE_LEAD}\n\n"
        f"I've picked up this {view_cmd} and I'm working it in a live session — "
        "reply here and I read your messages in-process (no mention needed "
        "while I'm live). If I pause or finish, a new comment from you wakes me "
        "again while this thread is labelled `agent`."
    )
    post_comment(view_cmd, issue, repo, with_runner_context(body))
    print("posted startup announcement", file=sys.stderr)


def pr_is_closed(repo: str, pr: str) -> bool:
    """True if the PR is merged or closed — the PR session's terminal state."""
    raw = gh("pr", "view", pr, "--repo", repo, "--json", "state", check=False)
    if not raw:
        return False
    try:
        return json.loads(raw).get("state", "OPEN") != "OPEN"
    except json.JSONDecodeError:
        return False


def linked_issue_for_pr(repo: str, pr: str) -> str | None:
    """Return the issue number this PR closes (from `Closes #N`), else None."""
    raw = gh("pr", "view", pr, "--repo", repo, "--json", "body", check=False)
    if not raw:
        return None
    try:
        body = json.loads(raw).get("body", "")
    except json.JSONDecodeError:
        return None
    m = re.search(r"\b[Cc]loses #(\d+)\b", body)
    return m.group(1) if m else None


def open_pr_for_issue(repo: str, issue: str) -> str | None:
    """Return the URL of an open PR that closes this issue, else None.

    The S/M path opens a PR whose body contains ``Closes #<issue>`` (the playbook
    guarantees this), so we match on that body text to find the PR.
    """
    raw = gh(
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--search",
        f"in:body Closes #{issue}",
        "--json",
        "url,body",
        check=False,
    )
    if not raw:
        return None
    try:
        prs = json.loads(raw)
    except json.JSONDecodeError:
        return None
    for pr in prs:
        # `in:body` is a loose full-text match; confirm the exact close keyword
        # so we do not link an unrelated PR that merely mentions the number.
        if re.search(rf"\bCloses #{re.escape(issue)}\b", pr.get("body", "")):
            return pr.get("url")
    return None


def pr_ref_in_text(pr_url: str, text: str) -> bool:
    """True if ``text`` references the PR by ``/pull/<n>`` or ``#<n>``.

    The DONE summary embeds the PR link as ``#<n>`` (playbook) or the full URL;
    this lets the wrapper avoid re-appending a link the summary already carries.
    """
    m = re.search(r"/pull/(\d+)", pr_url)
    if not m:
        return False
    pr_num = m.group(1)
    return bool(re.search(rf"(?:/pull/{re.escape(pr_num)}|#{re.escape(pr_num)})\b", text))


def issue_already_links_pr(repo: str, issue: str, pr_url: str) -> bool:
    """True if the agent already announced ``pr_url`` on the issue.

    Idempotency guard: survives resume/re-run so the link is posted exactly once.
    Matches any comment authored by the agent App (AGENT_BOT_LOGIN) that
    references THIS PR by either ``/pull/<n>`` or ``#<n>`` (see pr_ref_in_text).
    Author-scoping is what lets us drop the old in-body marker: the model's
    ``Opened #<n>`` note carried no marker, so a URL-only check missed it and
    double-posted.
    """
    raw = gh("issue", "view", issue, "--repo", repo, "--json", "comments", check=False)
    if not raw:
        return False
    try:
        comments = json.loads(raw).get("comments", [])
    except json.JSONDecodeError:
        return False
    for c in comments:
        author = (c.get("author") or {}).get("login", "")
        if author == AGENT_BOT_LOGIN and pr_ref_in_text(pr_url, c.get("body", "")):
            return True
    return False


PUSHGATEWAY_URL = os.environ.get(
    "PUSHGATEWAY_URL",
    "http://pushgateway.observability.svc.cluster.local:9091",
)


class UsageTracker:
    """Accumulates token/cost/turn totals across TurnUsage records and pushes
    them to the pushgateway after every turn, so a hard kill still leaves the
    latest numbers behind. Push failures are non-fatal — metrics must never
    break the agent.

    Metric names keep the `claude_agent_` prefix even though the backend is
    now provider-agnostic: renaming would orphan the existing Grafana series.
    Rename (with dashboard updates) as a deliberate follow-up, not in passing."""

    _TOKEN_KEYS = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )

    def __init__(self, issue: str, model: str) -> None:
        self.issue = issue
        self.model = model
        self.tokens = dict.fromkeys(self._TOKEN_KEYS, 0)
        self.cost_usd = 0.0
        self.num_turns = 0

    def record(self, usage: TurnUsage) -> None:
        for key in self._TOKEN_KEYS:
            self.tokens[key] += getattr(usage, key)
        self.cost_usd += usage.cost_usd
        self.num_turns += usage.num_turns
        self.push()

    def push(self) -> None:
        labels = f'{{model="{self.model}"}}'
        lines = [
            f'claude_agent_tokens_total{{model="{self.model}",type="{k}"}} {v}'
            for k, v in self.tokens.items()
        ]
        lines.append(f"claude_agent_cost_usd_total{labels} {self.cost_usd}")
        lines.append(f"claude_agent_turns_total{labels} {self.num_turns}")
        body = "\n".join(lines) + "\n"
        url = f"{PUSHGATEWAY_URL}/metrics/job/issue-agent/issue/{self.issue}"
        # S310: the URL scheme comes from PUSHGATEWAY_URL (cluster config),
        # always http(s) — never file: or a custom scheme.
        req = urllib.request.Request(  # noqa: S310
            url,
            data=body.encode(),
            method="PUT",
            headers={"Content-Type": "text/plain"},
        )
        try:
            urllib.request.urlopen(req, timeout=5).close()  # noqa: S310
        except (urllib.error.URLError, OSError) as exc:
            print(f"pushgateway push failed (non-fatal): {exc}", file=sys.stderr)

    def write_job_summary(self, status: str = "completed") -> None:
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if not path:
            return
        cache_denom = (
            self.tokens["cache_read_input_tokens"]
            + self.tokens["input_tokens"]
            + self.tokens["cache_creation_input_tokens"]
        )
        cache_ratio = self.tokens["cache_read_input_tokens"] / cache_denom if cache_denom else 0.0
        with open(path, "a", encoding="utf-8") as fh:
            # Run-record header: same source+status provenance as the terminal
            # comment (run_record()), so a scheduled/autopilot run is auditable
            # from the Actions summary too, not just the issue thread.
            fh.write(f"{run_record(status)}\n\n")
            fh.write(
                f"### Claude usage (issue #{self.issue}, {self.model})\n\n"
                f"| metric | value |\n|---|---|\n"
                f"| turns | {self.num_turns} |\n"
                f"| cost (USD) | {self.cost_usd:.4f} |\n"
                f"| input tokens | {self.tokens['input_tokens']} |\n"
                f"| output tokens | {self.tokens['output_tokens']} |\n"
                f"| cache write tokens | {self.tokens['cache_creation_input_tokens']} |\n"
                f"| cache read tokens | {self.tokens['cache_read_input_tokens']} |\n"
                f"| cache-hit ratio | {cache_ratio:.1%} |\n"
            )


# The generic triage-and-fix playbook baked beside this wrapper (image path
# /opt/issue-agent/default-playbook.md). Used when the target repo ships no
# playbook of its own — resolved by absolute path so the agent can Read it even
# though the session's cwd is the checked-out repo.
DEFAULT_PLAYBOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "default-playbook.md")


def default_branch_for(repo: str) -> str:
    """The PR base branch for ``repo``.

    ``AGENT_BASE_BRANCH`` overrides; otherwise ask GitHub (reliable even on a
    fresh/shallow ``actions/checkout`` where ``origin/HEAD`` may be unset),
    falling back to ``main`` if the query returns nothing.
    """
    override = env("AGENT_BASE_BRANCH")
    if override:
        return override
    branch = gh(
        "repo",
        "view",
        repo,
        "--json",
        "defaultBranchRef",
        "-q",
        ".defaultBranchRef.name",
        check=False,
    )
    return branch or "main"


def resolve_playbook(cwd: str) -> str:
    """Path the seed prompt points the agent at as its triage-and-fix playbook.

    Prefer the target repo's own playbook (``AGENT_PLAYBOOK``, default
    ``.claude/commands/triage-and-fix.md``) when it exists in the checkout, so a
    repo can tailor the flow (gitops-homelab does). Otherwise fall back to the
    generic ``DEFAULT_PLAYBOOK`` shipped with the agent, so a repo that enables
    the agent without authoring its own playbook still works out of the box.
    """
    rel = env("AGENT_PLAYBOOK", ".claude/commands/triage-and-fix.md")
    return rel if os.path.exists(os.path.join(cwd, rel)) else DEFAULT_PLAYBOOK


SEED_PROMPT = """\
You are the autonomous issue agent for the {repo} repo, running in a CI
runner with a LIVE multi-turn session. Read CLAUDE.md and follow the playbook at
`{playbook}` (open it with the Read tool). The target issue is
**#{issue}** in repo {repo}.

**HARD RULE — you are a non-interactive agent: NEVER push to `{base}`, NEVER
force-push, and NEVER merge a PR.** Every fix you make goes on a feature
branch and lands via a PR (`gh pr create --base {base}`); ONLY the human
merges it. Where `{base}` is protected by a server-side ruleset (PR required,
direct pushes rejected), a direct push or merge from this runner cannot succeed
regardless. No approving review is required — just open the PR and stop.

You control the conversation flow with these markers — emit them literally in
your reply text:

- When you need a human-only answer (intent, desired behaviour, a genuine fork
  between valid approaches, or unobservable facts) that you CANNOT determine by
  reading the repo: ask ALL your open questions at once, wrapped exactly as:
      <<<ASK>>>
      ...your numbered questions...
      <<<END_ASK>>>
  Then STOP your turn. Do NOT post the questions yourself with `gh issue comment`
  — the wrapper posts the text between the markers to the issue for you. Put
  ONLY the questions between the markers (no preamble, no narration). The user's
  reply will arrive as your next message in this same session — you keep all
  prior context, so do not re-read the thread.

- When you have finished (S/M: opened a ready (non-draft) PR; L/XL: decided the
  findings and applied labels, exactly as the playbook describes), end your reply
  with your reader-facing summary wrapped exactly as:
      <<<DONE>>>
      ...your final human-facing summary in markdown...
      <<<END_DONE>>>
  Put ONLY the reader-facing summary between the markers. The wrapper posts it to
  the issue as ONE comment with a "session ended" footer — do NOT post it yourself
  with `gh issue comment`. For S/M include the PR link (#<n> or the full URL) plus
  a 2-3 line root-cause / what-changed; for L/XL put your full findings here (apply
  labels with `gh issue edit` — that is an edit, not a comment, so keep doing it).

Do not ask about anything you can answer yourself from the repo. Be decisive
once you have enough information. Begin now: read the issue with
`gh issue view {issue} --repo {repo} --comments`, investigate (delegate
read-heavy exploration via the Task tool where the repo defines subagents for
it), then ask or act.
"""


PR_SEED_PROMPT = """\
You are the autonomous PR agent for the {repo} repo, running in a CI
runner with a LIVE multi-turn session. Read CLAUDE.md and any repo-specific
conventions in `.claude/`. A human invoked you (via the `agent` label or a
comment on the labelled PR) on **PR #{pr}** in repo {repo}. Its branch is
checked out in your working directory.

**HARD RULE — you are a non-interactive agent: NEVER push to `{base}`, NEVER
force-push, and NEVER merge a PR.** You may push new commits to THIS PR's
feature branch to address the human's request (`git push` to the current
branch, never `{base}`). ONLY the human merges.

You control the conversation flow with these markers — emit them literally:

- When you need a human-only answer you CANNOT determine from the repo/PR:
      <<<ASK>>>
      ...your numbered questions...
      <<<END_ASK>>>
  Then STOP. The wrapper posts the enclosed text as a PR comment; the reply
  arrives as your next message in this same session (full context retained).

- When you have addressed the request for now (answered, or pushed a commit),
  end your reply with your reader-facing summary wrapped exactly as:
      <<<DONE>>>
      ...your final human-facing summary in markdown (what you did / answered)...
      <<<END_DONE>>>
  Put ONLY the summary between the markers. The wrapper posts it as ONE PR comment
  with a "session ended" footer — do NOT post it yourself with `gh pr comment`.

The session stays alive across comments until the PR is merged, closed, or
times out. Begin now: read the PR and the comment thread with
`gh pr view {pr} --repo {repo} --comments` and `gh pr diff {pr} --repo {repo}`.
{issue_context}Investigate, then ask or act.
"""


async def run() -> int:
    repo = env("GITHUB_REPOSITORY", required=True)
    # TARGET_KIND selects the mode: "issue" (default) = the issue triage/fix
    # agent; "pr" = the PR-scoped interactive agent (agent-labelled PR).
    kind = env("TARGET_KIND", "issue")
    is_pr = kind == "pr"
    # The target number: ISSUE_NUMBER for issues, PR_NUMBER for PRs. `issue`
    # remains the variable name used throughout the loop (gh issue/pr view both
    # accept it) to keep the diff small.
    issue = env("PR_NUMBER" if is_pr else "ISSUE_NUMBER", required=True)
    # AGENT_PROVIDER selects the providers/ adapter; the model id is passed
    # through to it opaquely. claude-model: manual bump per Claude release (no
    # Renovate datasource tracks Anthropic model IDs). Overridden by
    # AGENT_MODEL in the workflows.
    provider_name = env("AGENT_PROVIDER", "claude")
    model = env("AGENT_MODEL", "claude-opus-4-8")
    max_runtime = int(env("MAX_RUNTIME_SECONDS", "2940"))
    poll_interval = int(env("POLL_INTERVAL_SECONDS", "20"))
    started = time.monotonic()

    sid = pr_session_id_for(repo, issue) if is_pr else session_id_for(repo, issue)
    provider = create_provider(provider_name)
    cwd = env("GITHUB_WORKSPACE", os.getcwd())
    # Repo-specific bits of the seed prompt, so the same runtime serves any repo:
    # the PR base branch and the playbook the agent should follow.
    base = default_branch_for(repo)
    playbook = resolve_playbook(cwd)

    # Decide fresh-vs-resume by probing the provider for THIS session's
    # persisted transcript (how it is keyed and stored is the adapter's business).
    resume = await provider.session_exists(sid, cwd)
    print(
        f"session {sid} provider={provider_name} resume={resume} model={model}",
        file=sys.stderr,
    )

    # Pass the role through so the provider can scope per-mode policy (the Claude
    # adapter narrows the tool allowlist for "pr" vs "issue"). `kind` is already
    # "issue"/"pr" from TARGET_KIND above.
    cfg = SessionConfig(model=model, cwd=cwd, session_id=sid, resume=resume, kind=kind)

    usage = UsageTracker(issue=issue, model=model)

    view_cmd = "pr" if is_pr else "issue"

    # Announce presence up front (idempotent) so a human sees an agent is on the
    # thread and knows a plain reply reaches it — no mention needed. Posted
    # once by the opener; resumes find it already there and skip.
    post_announcement(view_cmd, repo, issue)

    async with provider.open_session(cfg) as session:
        if resume:
            first_prompt = (
                "Resuming after an interruption. You retain full prior context "
                f"from this session. Re-check the latest {view_cmd} comments with "
                f"`gh {view_cmd} view {issue} --repo {repo} --comments` for "
                "anything new since you paused, then continue: ask "
                "(<<<ASK>>>...) or act (<<<DONE>>>) per your playbook."
            )
        elif is_pr:
            # Seed a fresh PR session with a pointer to the linked issue (if any)
            # so the agent can re-read that context; we do NOT resume the issue's
            # own transcript (new session, PR-scoped, by design).
            linked = linked_issue_for_pr(repo, issue)
            issue_context = (
                f"This PR closes issue #{linked}; read it with "
                f"`gh issue view {linked} --repo {repo} --comments` for the "
                "original request and any prior findings. "
                if linked
                else ""
            )
            first_prompt = PR_SEED_PROMPT.format(
                pr=issue, repo=repo, base=base, issue_context=issue_context
            )
        else:
            first_prompt = SEED_PROMPT.format(issue=issue, repo=repo, base=base, playbook=playbook)

        prompt: str | None = first_prompt
        while True:
            if time.monotonic() - started > max_runtime:
                # Out of budget: the transcript is already mirrored to MinIO by
                # the session_store, so just signal a clean pause and exit. The
                # note can name `@brujoand-agent` plainly: this comment is authored
                # by the agent App (author_association NONE), and the resume guard
                # only fires for OWNER/MEMBER/COLLABORATOR (or `brujoand`), so the
                # agent can never re-trigger itself regardless of the body.
                pause_note = (
                    ":hourglass: Paused (runtime budget reached). Reply here to "
                    "resume (or mention `@brujoand-agent` if this thread isn't "
                    "labelled `agent`) — I keep full context."
                )
                post_comment(
                    view_cmd,
                    issue,
                    repo,
                    with_runner_context(
                        f"{pause_note}\n\n{run_record('paused')}", "Was running in"
                    ),
                )
                # `agent-waiting` is an issue-only handoff signal; PRs have no
                # such label and the resume path there is any human comment.
                if not is_pr:
                    gh(
                        "issue",
                        "edit",
                        issue,
                        "--repo",
                        repo,
                        "--add-label",
                        "agent-waiting",
                    )
                print("runtime budget reached; paused", file=sys.stderr)
                usage.write_job_summary(status="paused")
                return 0

            turn = await session.run_turn(prompt)
            text = turn.text
            if turn.usage is not None:
                print(
                    f"turn done session={turn.session_id} "
                    f"turns={turn.usage.num_turns} err={turn.is_error}",
                    file=sys.stderr,
                )
                usage.record(turn.usage)

            if DONE_MARKER in text:
                # The reader-facing summary the model enclosed in the paired DONE
                # marker; a bare <<<DONE>>> (no <<<END_DONE>>>) yields "" and we
                # post a footer-only comment (graceful fallback).
                m = DONE_RE.search(text)
                summary = m.group(1).strip() if m else ""
                if not is_pr:
                    gh(
                        "issue",
                        "edit",
                        issue,
                        "--repo",
                        repo,
                        "--remove-label",
                        "agent-waiting",
                        check=False,
                    )
                    # Safety net (issue S/M only): fold the PR link into THIS
                    # closing comment if the summary omitted it — no separate
                    # comment. Skipped for L/XL (no PR) and when the summary or a
                    # prior agent comment already references the PR.
                    pr_url = open_pr_for_issue(repo, issue)
                    if (
                        pr_url
                        and not pr_ref_in_text(pr_url, summary)
                        and not issue_already_links_pr(repo, issue, pr_url)
                    ):
                        link = f"Opened {pr_url} to address this issue."
                        summary = f"{summary}\n\n{link}" if summary else link
                # ONE closing comment: the model's summary (if any) + a footer
                # making explicit the agent is NOT staying live to wait for a
                # reply on this DONE path (so open questions in a findings comment
                # don't read as "still standing by"). `@brujoand-agent` is safe to
                # name plainly here (see the pause-note comment above: author gate,
                # not body).
                ended_footer = (
                    ":checkered_flag: **Session ended — I'm no longer live on "
                    f"this {view_cmd}, so I'm not waiting for a reply here.** "
                    "To continue (answer a question, request a change), reply "
                    "here (or mention `@brujoand-agent` if this thread isn't "
                    "labelled `agent`) — I resume with full context from the "
                    "persisted transcript."
                )
                body = f"{summary}\n\n---\n{ended_footer}" if summary else ended_footer
                body = f"{body}\n\n{run_record('completed')}"
                post_comment(view_cmd, issue, repo, with_runner_context(body, "Ran in"))
                print("agent signalled DONE", file=sys.stderr)
                usage.write_job_summary()
                return 0

            ask = ASK_RE.search(text)
            if not ask:
                # No marker: nudge the agent to either ask or finish explicitly.
                prompt = (
                    "You did not emit an <<<ASK>>>...<<<END_ASK>>> block or "
                    f"{DONE_MARKER}. If you need input, ask now using the ASK "
                    "markers; otherwise finish the work and emit "
                    f"{DONE_MARKER}."
                )
                continue

            question = ask.group(1).strip()
            # Be explicit that a LIVE session is holding open for the answer, so
            # the human knows a reply here is consumed in-process (no mention
            # needed) rather than falling into the void. `@brujoand-agent` is safe
            # to name plainly (see the pause-note comment above: the resume guard
            # keys on author_association, not the comment body).
            waiting_note = (
                "\n\n---\n"
                ":green_circle: **Live session waiting for your reply.** I'm "
                "holding this session open and polling this thread every "
                f"~{poll_interval}s — reply here and I continue automatically "
                "in-process (no need to mention me while I'm live). "
                "If I hit my runtime budget first I'll post a pause note; after "
                "that, just reply here again to resume with full context (or "
                "mention `@brujoand-agent` if this thread isn't labelled `agent`)."
            )
            post_comment(
                view_cmd,
                issue,
                repo,
                with_runner_context(question + waiting_note, "Running in"),
            )
            asked_at = now_iso()

            # Block polling for the user's reply.
            reply: str | None = None
            while reply is None:
                if time.monotonic() - started > max_runtime:
                    break  # handled at top of loop
                # A PR session is terminal once the PR is merged/closed — stop
                # waiting on a reply that will never come.
                if is_pr and pr_is_closed(repo, issue):
                    print("PR merged/closed; ending session", file=sys.stderr)
                    usage.write_job_summary()
                    return 0
                await anyio.sleep(poll_interval)
                reply = latest_human_comment(repo, issue, asked_at)

            if reply is None:
                continue  # budget exceeded -> top of loop pauses cleanly

            prompt = (
                f"The user replied in the {view_cmd} thread:\n\n"
                f"{reply}\n\n"
                "Continue: ask more (<<<ASK>>>...) only if still genuinely "
                f"blocked, otherwise act and emit {DONE_MARKER}."
            )


def main() -> None:
    sys.exit(anyio.run(run))


if __name__ == "__main__":
    main()
