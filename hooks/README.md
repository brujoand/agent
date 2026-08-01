# Shared Claude hooks

One `*.sh` per hook, plus `hooks.json` saying where each one is wired. `agent
hooks install` symlinks the scripts into `~/.claude/hooks/` **and** merges their
declared entries into `~/.claude/settings.json`.

Both halves, always. Claude Code runs a hook because a `settings.json` names it
against an event and a matcher — not because the file exists. A symlink on its
own installs a silent no-op: present, inert, and indistinguishable from working
until you notice the thing it was supposed to do never happened. Keeping the
wiring in `hooks.json`, next to the script, means a hook's event and matcher are
reviewed in the same diff as its code.

`hooks.json` is Claude Code's own hooks schema with one substitution: `script`
names a file in this directory, and install renders it into the `command` the
user's settings need. Nothing here hardcodes `~/.claude`, so `CLAUDE_CONFIG_DIR`
keeps working.

A hook is the third way this repo ships behaviour, and the only one the model
never sees:

- a **skill** ([`../skills/`](../skills)) is opt-in — Claude reads the name and
  decides whether to load it;
- a **rule** ([`../rules/`](../rules)) is always on — in context before the first
  response, every session;
- an **output style** ([`../output-styles/`](../output-styles)) is spliced into
  the system prompt — register, before the conversation starts;
- a **hook** is not context at all. It is the harness running a command when
  something happens, which is where a check belongs the moment it can be made
  deterministic. Nothing to remember, nothing to skip.

Install never touches what it does not own: a real file already sitting at a
hook's name in `~/.claude/hooks/` is reported as a conflict and left alone, and
only settings entries pointing into that directory at a script of *ours* are
added, updated, or removed. Hand-wired hooks in the same file survive untouched.

## `require-unstacked-pr.sh`

Blocks `gh pr create` / `gh pr edit` when `--base` names anything but the repo's
default branch.

A stacked PR does not survive its parent being merged. Merges here are **squash**
merges with branch deletion, so the parent's commits never reach the default
branch under their original SHAs; the child is left with a base that no longer
exists, and GitHub either conflicts it or closes it. The work looks merged and
landed nowhere. Three pull requests were lost that way.

The rule that would have saved them constrains *merge order* — child first, or
rebase the child onto the default branch once the parent lands — and merge order
cannot be enforced from a hook, because only the human merges. So this enforces
the half an agent does control: the stack is never created. Deliberate stacks
stay reachable with `AGENT_ALLOW_STACKED_PR=1`, and the block message says to put
the ordering in the PR body, where review can see it. `agent inflight` reports
the stacks that already exist, which is the other half.

The default branch is read from `origin/HEAD` in the calling checkout, never
assumed — repos here use both `main` and `master`. Anything unparseable fails
**open**: no `--base`, no checkout, no `origin/HEAD`, or a command it does not
recognise all exit 0. `gh api` can set a base too and is deliberately not
matched; guessing at a REST body inside a shell string would block real work to
close a gap nobody has hit.

```bash
echo '{"tool_name":"Bash","cwd":"'"${PWD}"'","tool_input":{"command":"gh pr create --base feat/parent"}}' |
  hooks/require-unstacked-pr.sh --print
# block: feat/parent is not main
```

## `tmux-title.sh`

Renames the tmux window to a short camelCase summary of the current task — from
the first prompt of a session, and again from an accepted plan. Titles are sized
for a phone-width tab strip (three content words, 18 chars), because that tab
strip is often the only way to tell two sessions apart from a phone.

Derivation is pure bash over the hook payload — stopword-stripped, camelCased —
so it adds no latency and needs no model call. It no-ops entirely outside tmux.

```bash
echo '{"prompt":"fix the failing pre-commit check"}' | hooks/tmux-title.sh --print
# fixFailing
```

## `context-budget.sh`

Warns once when the session's resident context crosses 60% of the auto-compact
window, and again at 85%.

Why it earns a place in every turn's critical path: each turn re-sends the whole
conversation, so a session's cost is the sum of its context size over its turns —
quadratic in turn count, not linear. `agent usage` measured turns above 200k
context at 45% of turns but **76% of all re-read tokens**, about half of total
spend. The remedy is a new session at each task boundary, and the only thing that
makes that happen reliably is being told at the moment it matters.

The framing is deliberate: **`/clear` is free, compaction is not.** Compaction
reads the entire context to write a summary and drops detail on the way. So the
warning lands *before* the compaction window, while the free option is still the
one on the table.

The budget is `autoCompactWindow` from `settings.json` — one number to tune, not
two — falling back to the same value declared in
[`settings/settings.json`](../settings/settings.json), which a test asserts the
two files agree on. `AGENT_CONTEXT_BUDGET` overrides it per session.

Warnings go out as a `systemMessage`, which reaches the human only. Nothing else
may reach stdout: `UserPromptSubmit` stdout is injected into the model's context,
so a chattier version of this hook would spend tokens to warn about spending
tokens. It fires at most once per threshold per session, because a warning on
every prompt is a warning that gets ignored.

Reading is bounded to the transcript's tail, so it costs ~20ms even on a 13 MB
file. A missing, unreadable, or malformed transcript reports `none` rather than
failing the prompt.

```bash
printf '{"message":{"usage":{"cache_read_input_tokens":228000}}}\n' > /tmp/t.jsonl
echo '{"transcript_path":"/tmp/t.jsonl"}' | hooks/context-budget.sh --print
# high 228000 250000     <- level, resident tokens, budget
```

## `config-freshness.sh`

Warns at session start when this host is not loading the config that is on
origin. `agent freshness` is the whole implementation; the hook decides when to
ask and how to say it.

Why it needs a hook rather than a place in `agent doctor` (where it also lives):
the trees are symlinked *into* `~/src/agent`, which is what lets `agent pull`
update every session on the host with no reinstall — and the same property makes
staleness silent. A checkout twelve commits behind resolves every link cleanly,
so every per-tree probe passes while the rules actually loaded are the old ones.
Pulling alone is not sufficient either: it updates the contents of what is
already linked, but a skill or rule added upstream since install last ran has no
link at all. Neither failure announces itself, and both are fixed by a command
nobody thinks to run. So the check runs at the one moment it can still change
what the session does — before the first prompt.

It never waits on the network. The comparison is against the refs already on
disk, and a ref older than fifteen minutes triggers a *background* fetch so the
next session is accurate. The cost of that trade, stated plainly: a push to
origin can go unnoticed for one session. `agent doctor` fetches synchronously
instead, because that is the command you run when you want the real answer.

Output is a `systemMessage` — human only, never the model's context. A warning
about config drift is for the person who can run `agent pull`. Subagents get
their own `SessionStart`, so the hook exits early when the payload carries an
`agent_type`: one warning per session is a warning, one per subagent is noise.

```bash
echo '{"source":"startup"}' | hooks/config-freshness.sh --print
# (empty when current; one line naming the drift and the fix when not)
```

## `config-freshness.sh` vs `repo-freshness.sh`

Two hooks on the same event, two different repos. `config-freshness.sh` asks
whether `~/.claude` is loading what this repo says it should. `repo-freshness.sh`
asks whether the repo the session is *working in* is current, and pulls it.

## `repo-freshness.sh`

Fast-forwards the checkout the session started in, before it reads anything.

Staying current has two halves and only one was covered. `agent workspace create`
already fetches and fast-forwards the default branch before cutting a worktree,
so **implementation** always starts from a fresh `origin/<default>`.
**Exploration** did not: a session that opens `~/src/<repo>` and starts grepping
works against whatever was last on disk, which may be days old. A stale tree is
worse than no tree, because the answers look right.

`agent pull --here` is the whole implementation — one checkout, one fetch,
`--ff-only`. Three guards make it safe to run unattended: only a clean tree, only
on the default branch, only fast-forward. A feature branch is skipped silently, a
dirty tree is reported and left alone, and a diverged branch is reported as not
fast-forwardable. Worktrees are excluded by construction — they live under
`~/worktrees/`, outside the src root this resolves against, so a branch with work
on it is never a candidate.

Silent when there was nothing to do, which is the common case. Subagents inherit
the session's cwd and would each re-ask the same question, so the hook exits
early on an `agent_type` payload.

```bash
echo '{"source":"startup","cwd":"'"$HOME"'/src/agent"}' | hooks/repo-freshness.sh --print
# demo: pulled 1 commit from origin/main     <- or empty when already current
```

## `inflight.sh`

Tells a starting session what is already in flight on the repo it opened in:
open pull requests, and session worktrees already on disk.

This is a different question from the two above, and the case that bought it was
expensive. On 2026-07-29 an agent session opened PR #59 raising the auto-compact
window to 450k. On 2026-08-01 another session was asked for the same change,
checked that its checkout was current — it was — wrote the change from scratch as
#63, and merged it. #59 was left permanently conflicted: every line it touched
had been changed underneath it, so it could not be rebased, only abandoned.

**Pulling `main` would not have prevented that.** The base *was* current. The
duplicated work was never on `main` — it sat in an open pull request, which is
exactly where `git` cannot see it. So this asks GitHub.

Unlike its two siblings it writes to the **model's** context
(`hookSpecificOutput.additionalContext`), not to a `systemMessage`. The asymmetry
is the whole design: the human is not the actor about to open a duplicate PR. An
agent that cannot see #59 will write #59 again no matter what the terminal says.

It also names any **stack**: an open PR based on another open PR's branch gets a
`STACKED:` line stating which one has to merge first, because merging the parent
first drops the child (see `require-unstacked-pr.sh`).

That is a real context cost, so it is bounded — ten PRs, four files each, one
line apiece, and nothing at all when the repo is quiet. Truncation is always
reported (`+N more`) rather than silent. Advisory only: no credentials, no
network, no `gh`, or an older CLI all produce silence and exit 0, because a
session must start whether or not GitHub answers.

```bash
echo '{"source":"startup","cwd":"'"$HOME"'/src/agent"}' | hooks/inflight.sh --print
# Work already in flight on this repo — check for overlap BEFORE writing code...
#   #59 [chore/compact-window-450] chore(settings): raise the auto-compact window to 450k — touches settings/settings.json +2 more
```
