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
- a **hook** is not context at all. It is the harness running a command when
  something happens, which is where a check belongs the moment it can be made
  deterministic. Nothing to remember, nothing to skip.

Install never touches what it does not own: a real file already sitting at a
hook's name in `~/.claude/hooks/` is reported as a conflict and left alone, and
only settings entries pointing into that directory at a script of *ours* are
added, updated, or removed. Hand-wired hooks in the same file survive untouched.

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
two — falling back to 250k. `AGENT_CONTEXT_BUDGET` overrides it per session.

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
