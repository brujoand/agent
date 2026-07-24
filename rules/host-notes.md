# Host notes (brujoand agent host)

<!-- Managed by `agent rules install`. Edit this file in the agent repo and open a
     PR; never edit the copy imported into ~/.claude/CLAUDE.md. -->

Facts about this machine, true in every session — including worktrees under
`~/worktrees/`, which sit outside `~/src` and so never load `~/src/CLAUDE.md`.
Repo-specific rules live in each repo's own `CLAUDE.md`; this file never
overrides them.

## Task worktrees

**The primary checkouts under `~/src` are read-only for agents.** This holds for
*every* repo in the workspace, not just the ones with a `CLAUDE.md`. Every change
lands via worktree → feature branch → PR:

```bash
cd "$(agent workspace create <type>/<slug> [--repo <name>])"   # prints only the path
agent workspace delete <slug>    # refuses if the tree has uncommitted work
```

`--repo` defaults to `gitops-homelab`. `create` reads `origin/HEAD`, so it picks
up each repo's default branch (`main` or `master`) on its own — never assume.
It also runs `mise trust`; new worktrees are untrusted and the mise-shimmed tools
(`uv`, `shellcheck`, `shfmt`, `pre-commit`, `gh`) fail until trusted.

Read-only work in `~/src` is fine. There is **no trivial-one-liner exception**:
main/master is protected everywhere, so even a one-word fix needs a branch and a
PR — and the branch belongs in a worktree, so the primary checkout stays clean on
the default branch and parallel tasks never collide.

This is enforced, not advised. `~/.claude/hooks/require-worktree.sh` is a
`PreToolUse` hook (registered in `~/.claude/settings.json`) that blocks Edit /
Write / NotebookEdit and mutating git (`commit`, `push`, `checkout -b`, `sed -i`,
…) whose target resolves inside `~/src/<repo>`. It resolves `cd`, `git -C`, and
literal paths, so it is not fooled by a subshell. Being conservative, it also
blocks a harmless command that merely *mentions* such a path next to a git verb.

- Override (human, rare): `AGENT_ALLOW_PRIMARY_WRITE=1`.
- Self-test: `AGENT_ALLOW_PRIMARY_WRITE=1 ~/.claude/hooks/test-require-worktree.sh`.
- GitHub-hosted runs (CI, the issue agent) already work in a fresh isolated
  checkout on their own branch. They must **not** create worktrees, and the hook
  never applies there — it lives in `~/.claude`, which CI does not load.

## A worktree is spent once its PR merges

**Never build on a branch whose PR is already merged.** Resuming an old worktree
puts you on a branch that was merged days ago; commits made there go nowhere,
because pushing does **not** reopen a merged PR. The work sits on a dead branch
and looks done when it is not. A merged worktree is spent — delete it and cut a
new one. Do not "just add one more commit".

Merges here are **squash** merges, so the branch tip is *not* an ancestor of the
default branch: plain git ancestry misses most merged branches (13 of the 16
worktrees found on disk). The PR state is the only reliable signal.

`~/.claude/hooks/require-fresh-branch.sh` (`PreToolUse`) enforces it: before any
`git commit`/`push`/`cherry-pick`/`gh pr create` — and before any Edit/Write — it
resolves the target worktree's branch and blocks if the branch is already merged
or closed. It checks, cheapest first: a cached verdict (MERGED is terminal, so a
hit costs nothing), then local ancestry (free, catches merge-commit merges), then
GitHub via `gh`. Verdicts cache under `~/.claude/state/branch-status/`.

- `gh`'s stored token is stale. The live one is `GH_TOKEN=$(agent github token)` —
  the hook mints it itself.
- Infra failures (offline, no token) **fail open** so a flaky network cannot wedge
  a session; the local ancestry check still applies.
- Override (human, rare): `AGENT_ALLOW_MERGED_BRANCH=1`.
- Self-test: `AGENT_ALLOW_PRIMARY_WRITE=1 ~/.claude/hooks/test-require-fresh-branch.sh`.

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
