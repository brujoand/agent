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
