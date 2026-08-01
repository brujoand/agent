# Host notes (brujoand agent host)

<!-- Managed by `agent rules install`. Edit this file in the agent repo and open a
     PR; never edit the copy imported into ~/.claude/CLAUDE.md. -->

Facts about this machine, true in every session — including worktrees under
`~/worktrees/`, which sit outside `~/src` and so never load `~/src/CLAUDE.md`.
Repo-specific rules live in each repo's own `CLAUDE.md`; this file never
overrides them.

## Public by default: never leak internal infra

**Most repos in this workspace are world-readable; private is the exception.**
Before writing anything infra-shaped, know which kind you are in — **assume
public until you have checked**:

```bash
GH_TOKEN=$(agent github token) gh repo view <owner>/<repo> --json visibility -q .visibility
```

In a **public** repo, none of the following may appear — not in code, docs,
`CLAUDE.md`, comments, tests, fixtures, sample config, commit messages, PR
bodies, or issue comments:

1. **The maintainer's private domain** — any hostname, subdomain, or URL under
   it.
2. **Cluster-internal addressing** — `*.svc.cluster.local`, RFC1918 addresses
   (`10.*`, `192.168.*`, `172.16–31.*`), node names, control-plane endpoints.
3. **How the private infrastructure is built** — GitOps layout, k8s namespaces
   and workload names, runner/bucket/secret-store names, App identity or
   installation IDs, storage paths, network topology.
4. **Where secrets live** — dotfile paths, password-manager item and field
   names, ExternalSecret keys, secrets-file locations.
5. **Private repositories** — never name or link one from a public repo. Even a
   dead link tells a reader it exists.

The concrete denylist — the actual domain, address ranges, and private repo
names — lives in `~/.claude/CLAUDE.md`, which is local and never committed. That
separation is the point: this file is itself published, so it states the policy
and not the values.

The fix is not redaction, it is **genericization**: read the value from an env
var, flag, or config file, and use `example.com` / `<your-domain>` /
`$CLUSTER_DOMAIN` in docs and defaults. If a public tool needs a homelab-shaped
example, invent one.

**Why:** public repos are indexed, forked, and archived (GitHub code search,
GHArchive). A leak is permanent — a force-push rewrite does not remediate it
(old SHAs stay reachable; forks and caches keep copies) and needs admin, which
agents do not have. Prevention is the only real control.

**Reviews must check this.** Any code review — `/code-review`, a review
subagent, or the `pr-review` workflow — running against a public repo treats an
internal-infra reference as a finding in its own right, not a nit. Check the diff
against all five categories before signing off. The reusable `pr-review` workflow
passes repo visibility into its prompt and asks for this explicitly.

## Task worktrees

**Primary checkouts under `~/src` are read-only for agents, and a worktree is
spent once its PR merges.** Every change lands via worktree → feature branch →
PR: never in `~/src`, never on the default branch, and never on a branch whose PR
has already merged — pushing does not reopen it, so the work lands nowhere while
looking done. There is no trivial-one-liner exception. Read-only work in `~/src`
is fine.

```bash
cd "$(agent workspace create <type>/<slug> [--repo <name>])"   # prints only the path
agent workspace delete <slug>    # refuses if the tree has uncommitted work
```

`--repo` defaults to `gitops-homelab`. `create` reads `origin/HEAD`, so it picks
up each repo's default branch (`main` or `master`) on its own — never assume.
It also runs `mise trust`; new worktrees are untrusted and the mise-shimmed tools
(`uv`, `shellcheck`, `shfmt`, `pre-commit`, `gh`) fail until trusted.

Both halves are enforced, not advised, by `PreToolUse` hooks in `~/.claude/hooks/`:
`require-worktree.sh` and `require-fresh-branch.sh`. They block the offending call
and print the recovery commands, so the mechanics live there rather than here.
Because merges are squashes, branch freshness is judged by PR state via `gh`, not
by git ancestry. Overrides (human, rare) are `AGENT_ALLOW_PRIMARY_WRITE=1` and
`AGENT_ALLOW_MERGED_BRANCH=1`; each hook has a `test-*.sh` beside it.

**GitHub-hosted runs** (CI, the issue agent) already work in a fresh isolated
checkout on their own branch. They must **not** create worktrees — and the hooks
never apply there, because they live in `~/.claude`, which CI does not load.

## Never stack a pull request

**A PR's base is the default branch.** Basing one PR on another open PR's branch
loses it: merges here are squashes with branch deletion, so the parent's commits
never reach the default branch under their original SHAs, and the child is left
with a base that no longer exists — GitHub conflicts it or closes it, and the
work looks merged while having landed nowhere. That has cost three PRs.

If a stack is genuinely unavoidable, the order is not optional: **merge the child
BEFORE the parent, or switch the child's base to the default branch once the
parent lands.** Merging parent-first drops the child every time.

`require-unstacked-pr.sh` blocks `gh pr create|edit --base <not-the-default>`;
override with `AGENT_ALLOW_STACKED_PR=1` and state the merge order in the PR
body. `agent inflight` (and its session-start hook) reports any stack that
already exists, so the ordering is visible before either one is merged.

## Secrets: there is no 1Password here

This host has no `OP_SERVICE_ACCOUNT_TOKEN` and never will. `op` and
`lab 1password …` will fail. Agent secrets are pre-baked into `~/.bash_private`
by `lab agent bootstrap`.

**If you need a secret you don't have, ask the human** — state the exact
1Password item and fields, and what for. Do not try to reach 1Password, edit
`~/.bash_private`, or otherwise widen your own access. You can't, by design.

## Git auth

`agent` is its own git credential helper. It points at the *installed* copy in
`~/.local/bin`, never `./agent` in a checkout — a helper living in tracked files
is a bootstrap trap. `credential.helper` is multi-valued; to test one in
isolation, reset first with `-c credential.helper=`. `agent doctor` flags stale
helpers. Escape hatch needing no auth: `git merge --ff-only origin/<default>`.

## These rules themselves

They are tracked in `brujoand/agent` under `rules/`, and imported into
`~/.claude/CLAUDE.md` by `agent rules install`. User-level memory loads in every
session in every directory, worktrees included — which is why host-wide facts
live here and not in `~/src/CLAUDE.md`. `agent pull` picks up changes; no
reinstall. `agent doctor` reports whether the import block is current.
