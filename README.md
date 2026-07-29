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
3. A **`CLAUDE_CODE_OAUTH_TOKEN`** Actions secret (`claude setup-token`) — export
   it and `onboard.sh` sets it per repo (see below).

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

### `RENOVATE_BYPASS_APP_ID` and `RENOVATE_INSTALLATION_ID`

The `protect-main-pr-only` ruleset exempts two actors: the built-in Repository
admin role, and — so its automerge survives — the Renovate App. An `Integration`
bypass actor is identified by the **App id**, which is global to the App (unlike
an installation id), so one value is correct on every repo. This repo is public,
so that id is not committed: the definition carries `${RENOVATE_BYPASS_APP_ID}`
and you supply it.

```bash
export RENOVATE_BYPASS_APP_ID=<your Renovate App id>     # App settings -> "App ID"
export RENOVATE_INSTALLATION_ID=<its installation id>    # /settings/installations/<id>
agent setup rulesets --repo owner/repo                   # dry-run; --apply to write
./onboard.sh owner/repo
```

Both consumers fail loudly when the App id is unset rather than dropping the
actor. That matters: `bypass_actors` is replaced wholesale on write, so an
omitted actor is not left alone — it is **revoked**. If you do not run Renovate,
point the variable at whichever App you want exempt, or delete the entry from
the definition.

**The two variables are a pair.** GitHub rejects a bypass actor naming an App
that is not installed on the repo, and reports it as a bare
`422 Validation Failed` with no indication of which field is at fault. So
`onboard.sh` adds the repo to Renovate's installation *before* it writes the
ruleset; `RENOVATE_INSTALLATION_ID` is what lets it. That install is a
precondition for a valid ruleset, not an optional extra.

It earns its keep twice over: `packages: read` on the Renovate App only reaches
packages owned by repos inside its installation, so the same step is what lets
Renovate resolve that repo's private GHCR images instead of silently returning
`no-result`.

Note `agent setup rulesets` writes the ruleset but installs nothing, so on a repo
where Renovate is absent it hits the same 422. Onboard first.

## The `CLAUDE_CODE_OAUTH_TOKEN` secret (`onboard.sh`)

The agent authenticates to Anthropic with a `CLAUDE_CODE_OAUTH_TOKEN` Actions
secret on each repo. `onboard.sh` sets it as part of onboarding, from your
environment — so there is nothing separate to run:

```bash
claude setup-token                                # interactive; copy the token it prints
read -rs CLAUDE_CODE_OAUTH_TOKEN                   # paste it (hidden, no shell history)
export CLAUDE_CODE_OAUTH_TOKEN
./onboard.sh owner/repo                            # installs the App, ruleset, and sets the secret
```

`gh secret set` is an upsert, so this adds the secret if absent and overwrites it
if present — which makes **rotation just a re-onboard** with a fresh token
exported. Leave `CLAUDE_CODE_OAUTH_TOKEN` unset and onboarding skips that step
rather than clobbering a good secret with a blank.

There is deliberately no cross-repo fan-out: a human token cannot enumerate where
the App is installed (GitHub only allows that with the App's own credentials —
see the note in `onboard.sh`), and onboarding already names the repo.

## Release → deploy bump (`onboard.sh`)

A repo that publishes a container image can tell a deployment receiver about it
directly, so the deploy PR opens as soon as the image exists instead of waiting
for something to poll the registry. Onboarding hands the repo the two values its
release workflow needs, as Actions secrets:

```bash
export RELEASE_BUMP_WEBHOOK_URL=https://receiver.example.com/hook/release-bump
read -rs RELEASE_BUMP_WEBHOOK_SECRET && export RELEASE_BUMP_WEBHOOK_SECRET
./onboard.sh owner/repo        # sets RELEASE_BUMP_URL + RELEASE_BUMP_SECRET
```

The workflow then POSTs `{app, tag, digest}` signed with HMAC-SHA256 in
`X-Hub-Signature-256`, plus `X-Bump-Source: release`. Sending the digest means
the receiver never has to read the registry back — which it cannot do for a
private package anyway.

**Why the repo calls instead of a webhook.** This started as a `registry_package`
webhook that `onboard.sh` registered per repo. GitHub delivered two consecutive
releases about 40 minutes late and in the wrong order, so the older tag was
applied last and the deployment was rolled backwards. Webhook delivery is a queue
you neither control nor can order. A step in the release job runs after the push
it reports, retries on its own, and fails the release when it cannot deliver — so
a missed bump is loud rather than silently stale. Onboarding deletes a leftover
webhook on the way past, since a repo carrying both would bump twice.

Both variables come from your environment and are never hardcoded here: this repo
is public, and keeping them parametric also means a fork points at its own
receiver. Set neither and the step is skipped; there is no half-configured state,
because a URL without a key just produces signed bodies nobody accepts.

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
| `model` / `AGENT_MODEL` | `claude-opus-5` | model id passed to the provider |
| `session_store_endpoint`/`_bucket` + `AWS_*` | *(empty → stateless)* | MinIO/S3 for transcript persistence + cross-timeout resume |
| `otlp_metrics_endpoint` | *(empty → off)* | OTLP base URL for usage telemetry. The SDK spawns the `claude` CLI with this process's env, so the CLI's own OpenTelemetry exporter reports `claude_code.*` metrics tagged `app.entrypoint=sdk-py` — the same series an interactive session produces. A Prometheus OTLP receiver requires cumulative temporality; the workflow sets it. |
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
| `agent gh <args...>` | run the GitHub CLI with GH_TOKEN already minted |
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
agent rules install             # import the always-on Claude rules into ~/.claude/CLAUDE.md
agent rules list                # show each shared rule and whether the import block is current
agent hooks install             # link the shared Claude hooks AND wire them into settings.json
agent hooks list                # show each shared hook, where it is wired, and whether it is linked
agent install                   # all three of the above at once (--lab to add the lab CLI)
```

### Shared Claude skills, rules, and hooks

Three trees, one source of truth each, all tracked and PR-reviewed here, and all
installed **once per user** — covering every repo and every worktree, since they
all read the same `~/.claude`. All three point at this checkout, so `agent pull`
fast-forwarding the agent repo updates them in place — no reinstall, no drift.
`agent doctor` reports on all three, every install is idempotent, and `agent
install` runs them together for a fresh host.

| | `skills/` | `rules/` | `hooks/` |
|---|---|---|---|
| Loaded | when Claude picks the skill | every session, everywhere | when its event fires |
| Installed as | symlinks into `~/.claude/skills/` | `@`-imports in `~/.claude/CLAUDE.md` | symlinks into `~/.claude/hooks/` **+ entries in `~/.claude/settings.json`** |
| Command | `agent skills install` | `agent rules install` | `agent hooks install` |
| For | a task procedure, loaded on demand | house style and host facts | a deterministic reaction to an event |

The skill/rule split is the whole point: a skill is **opt-in**, so it is the
wrong home for anything that must shape the *first* response.
`working-with-brujoand` lived in `skills/` and was merely available — a session
that never invoked it never followed it. As a rule it is simply there, at the
cost of carrying it in every session, which is what always-on means. A hook is
neither: it is not in the model's context at all, it is the harness running a
command when something happens — which is where a check belongs once it can be
made deterministic.

Hooks are the one tree that needs two halves installed. Claude Code runs a hook
because some `settings.json` *names* it, not because the file exists, so a
symlink alone installs a silent no-op. Each hook's event and matcher therefore
live in `hooks/hooks.json`, reviewed in the same diff as the script, and
`hooks install` merges them into the user's settings.

No install clobbers what the user owns: a hand-made skill directory or hook file
is reported as a conflict and left alone, `rules install` only ever rewrites text
between its own markers in `~/.claude/CLAUDE.md`, and `hooks install` only ever
touches settings entries pointing at a script of ours — a hand-wired hook in the
same file is left exactly as it is.

`agent` clones sibling repos and installs `lab`, so the dependency points one way
— **`agent` → `lab`, never back** (`agent` is what puts `lab` on disk). github.com
is reached over HTTPS as the App everywhere (no SSH/deploy keys); every clone gets
`credential.https://github.com.helper = !~/.local/bin/agent git-credential`,
pointing at the *installed* copy — a helper living in tracked files is a bootstrap
trap. Escape hatch if you ever wedge auth: `git merge --ff-only origin/main` needs
none.

## License

MIT — see [LICENSE](LICENSE).
