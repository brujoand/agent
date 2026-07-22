# agent

My personal GitHub **issue agent**: label an issue and a live Claude session
triages it, asks any clarifying questions right on the thread, and opens a ready
pull request — then reviews PRs on the way back in.

> **This repo is public out of necessity, not as a product for you to use.** Some
> of my public repos call its reusable workflows, so it has to be reachable —
> hence public. It is built to fit *my* setup and needs exactly, and I'll change
> or break it whenever that suits me, without notice. Read it or fork it as a
> reference if it's useful, but don't depend on it — **make your own**. Something
> like this earns its keep precisely by catering to *you* the way this caters to
> me. The rest of this README is how it works (and, implicitly, how you'd wire up
> your own), not an invitation to adopt this one.

It is built from three parts:

- **`issue_agent/`** — the runtime: a small wrapper around the
  [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) that
  holds one live, multi-turn session per issue/PR. Baked into a container image.
- **`.github/workflows/*.reusable.yml`** — reusable GitHub Actions workflows a
  consumer repo calls with a ~10-line caller. The job body (mint a token,
  checkout, run the agent) lives here once, for every repo.
- **`agentcli/`** — the `agent` CLI: mints short-lived GitHub App tokens and
  enables the agent on a repo (`agent issue enable`).

> The runtime backend sits behind a provider seam (`AGENT_PROVIDER`, default
> `claude`), so the harness itself is provider-agnostic.

## What it does

- **Triage & fix.** Label an issue `agent` (or mention `@your-app` to add the
  label) and the agent investigates the repo, then either asks you questions or
  opens a ready PR that `Closes #<n>`. Large/risky work is scoped and reported
  instead of auto-changed — it never merges; only a human does.
- **Live conversation.** While the job runs it polls the thread and continues
  in-process — reply and it picks up, no re-mention needed. If it hits the job
  timeout it persists the transcript (when a session store is configured) and
  resumes on your next comment.
- **PR review, opt-out.** It reviews every non-draft PR and posts one verification
  comment — **unless** the PR carries an `auto-merge` label. Tag the PRs you are
  auto-merging; everything else is reviewed by default.
- **Hard rule.** The agent is non-interactive: it never pushes to the default
  branch, never force-pushes, never merges. Every change lands via a PR.

## Enable it on a repo

Prerequisites (all one-time, and human — the App can't grant itself these):

1. A **GitHub App** you control, installed on the target repo, with permissions:
   Metadata (R), Contents (R/W), Issues (R/W), Pull requests (R/W),
   Workflows (R/W). Its private key is mounted into your runner (never committed).
2. A **runner** the workflows run on (`ubuntu-latest`, or a self-hosted label)
   that has the runner image's tools — the `agent` CLI (to mint the token before
   checkout), the Claude Code CLI, and the `issue_agent` runtime. Build it from
   the `Dockerfile`, or use a published `agent-runner` image.
3. A **`CLAUDE_CODE_OAUTH_TOKEN`** repo/org Actions secret (`claude setup-token`).

Then, from a checkout with the App credentials available:

```bash
agent issue enable owner/repo                 # dry-run: shows the plan + a checklist
agent issue enable owner/repo --apply --open-pr
```

`enable` creates the `agent` / `agent-waiting` labels, opens a PR adding the thin
caller workflows, and lays down a **baseline `.pre-commit-config.yaml` + CI**
(see below). It auto-detects your App's login (so the runtime tells its own
comments apart from a human's) and prints the human-only steps it can't do
(the OAuth secret, runner availability, branch protection via
`agent setup rulesets --repo owner/repo`, and — only if your reusable-workflow
repo is private — granting it Actions access).

Point callers at your own fork of this repo with `--reusable-repo owner/agent`
(or `$AGENT_REUSABLE_REPO`).

## Rotating the shared `CLAUDE_CODE_OAUTH_TOKEN`

`claude setup-token` tokens expire, and once one does *every* agent run fails to
authenticate (the SDK returns `401 Invalid bearer token`). GitHub never lets you
read an Actions secret back, so there is no "copy it from one repo to the rest" —
you mint a fresh value and push it everywhere the App is installed:

```bash
claude setup-token | scripts/sync-agent-secret.sh CLAUDE_CODE_OAUTH_TOKEN
scripts/sync-agent-secret.sh CLAUDE_CODE_OAUTH_TOKEN --dry-run   # preview targets
```

Target repos come from `agent repos` (the App's own installation list). Writing
uses `$GH_TOKEN` if set (an App token with `secrets: write`), else your `gh
auth` session. See the script header for the full contract.

## Hygiene: the internal-infra denylist

The bundle `enable` adds includes a `no-internal-infra` pre-commit hook (plus
gitleaks and the usual basics, run in CI). It **refuses commits** that leak
internal infrastructure — cluster-internal `*.svc.cluster.local` DNS and RFC1918
private IPs — so that config never reaches a public repo in the first place.

No private domain is baked into this repo. Add your own at generate time:

```bash
AGENT_DENYLIST_EXTRA='example\.internal|corp\.example\.com' \
  agent issue enable owner/repo --apply --open-pr
```

The bundle is **no-clobber**: if a repo already has a `.pre-commit-config.yaml`
it is left untouched.

## Configuration

Runtime env / reusable-workflow inputs (all optional unless noted):

| Input / env | Default | Purpose |
|---|---|---|
| `bot_login` / `AGENT_BOT_LOGIN` | *(auto-detected by `enable`)* | the App's login; how the agent recognizes its own comments |
| `runner` | `ubuntu-latest` | `runs-on` label for the jobs |
| `model` / `AGENT_MODEL` | `claude-opus-4-8` | model id passed to the provider |
| `session_store_endpoint`/`_bucket` + `AWS_*` | *(empty → stateless)* | MinIO/S3 for transcript persistence + cross-timeout resume |
| `metrics_pushgateway` / `PUSHGATEWAY_URL` | *(empty → off)* | Prometheus pushgateway for per-run token/cost metrics |
| `AGENT_BASE_BRANCH` | *(repo default branch)* | PR base branch |
| `AGENT_PLAYBOOK` | `.claude/commands/triage-and-fix.md` | repo playbook; falls back to a generic one shipped with the agent |

A repo can tailor the agent by committing its own
`.claude/commands/triage-and-fix.md` and subagents — the session loads the target
repo's `CLAUDE.md` and `.claude/`.

## CLI reference

| Command | Purpose |
|---|---|
| `agent issue enable owner/repo [--apply] [--open-pr] [--reusable-repo] [--ref]` | enable the agent on a repo |
| `agent github token [--refresh]` | print a short-lived App installation token |
| `agent git-credential get` | git credential helper (mints a token on demand) |
| `agent repos` | HTTPS clone URLs the App installation can reach |
| `agent setup rulesets --repo owner/repo [--apply]` | converge branch protection (human-only) |
| `agent doctor` | check creds, token, reachable repos, credential helpers |

## Development

```bash
mise exec -- uv run pytest
mise exec -- uv run ruff check . && mise exec -- uv run ruff format --check .
```

The App-token mint is implemented twice on purpose — here in `agentcli/github.py`
and as a standalone script baked into the runner image (so it can run before
`actions/checkout`). Neither can call the other; keep the `iat` backdate, the
`exp` window, and the retry/fast-fail classification in sync.

---

## Maintainer host-glue (single-tenant — not part of the reusable tool)

This repo doubles as the maintainer's dev-host CLI, wired to one specific setup:
a flat `~/src` of sibling checkouts, session worktrees under `~/worktrees`, a
sibling `lab` CLI (from the maintainer's GitOps repo), and credentials in
`~/.bash_private`. These commands assume that layout and **won't run elsewhere
without edits** — they are not needed to use the issue agent above.

```bash
agent pull                      # clone/fast-forward every reachable repo into ~/src
agent workspace create <t>/<s>  # a worktree off fresh origin/<default>
agent workspace delete|list|gc  # manage session worktrees
agent lab install               # install `lab` from the sibling gitops repo
agent lab <args...>             # run `lab` with GH_TOKEN/KUBECONFIG set
agent skills install            # symlink the shared Claude skills into ~/.claude/skills
agent skills list               # show each shared skill and whether it is linked
```

### Shared Claude skills

`skills/` holds the workspace's shared Claude Code skills — one source of truth,
tracked and PR-reviewed here. `agent skills install` symlinks each into the
user's `~/.claude/skills/`, so **one install per user** covers every repo and
every worktree (they all read the same dir). The links point at this checkout,
so `agent pull` fast-forwarding the agent repo updates a skill in place — no
reinstall, no drift. `agent doctor` reports whether they are linked. Install is
idempotent and never clobbers a skill a user placed there by hand.

`agent` clones sibling repos and installs `lab`, so the dependency points one way
— **`agent` → `lab`, never back** (`agent` is what puts `lab` on disk). github.com
is reached over HTTPS as the App everywhere (no SSH/deploy keys); every clone gets
`credential.https://github.com.helper = !~/.local/bin/agent git-credential`,
pointing at the *installed* copy — a helper living in tracked files is a bootstrap
trap. Escape hatch if you ever wedge auth: `git merge --ff-only origin/main` needs
none.

## License

MIT — see [LICENSE](LICENSE).
