# Shared Claude output styles

One `<name>.md` per style, each a markdown body with Claude Code frontmatter.
`agent output-styles install` symlinks them into `~/.claude/output-styles/`.
Loose files here (this one included) are ignored.

This is the fifth way the repo ships behaviour, and the five differ in *where
the text lands*:

- a **skill** is opt-in — Claude reads its name and description and decides
  whether to load the body (`../skills/`),
- a **rule** is always-on memory, imported into `~/.claude/CLAUDE.md`
  (`../rules/`),
- a **hook** is a deterministic reaction to an event (`../hooks/`),
- a **setting** is a value in `settings.json` (`../settings/`),
- an **output style** is spliced into the **system prompt**.

That last one is why house style lives here rather than in `../rules/`. A rule
arrives as content, in the same channel as the task, and competes with it for
attention — which is right for facts about the host and wrong for register. An
output style shapes the voice before the first token of the conversation exists.

`keep-coding-instructions: true` keeps Claude Code's default coding instructions
in the system prompt alongside the style, so a style tunes voice without
throwing the harness away. Leave it on unless you are deliberately replacing the
coding behaviour too.

## Installing a style does not select it

The active style is the `outputStyle` key in `settings.json`, declared in
`../settings/settings.json` and converged by `agent settings install`. Same
two-halves split as the hooks tree: this tree ships the style, the settings
declaration decides which one is on. `agent install` runs the styles before the
settings for exactly that reason.

```bash
agent output-styles install     # link the tree
agent output-styles list        # per-style link state
agent doctor                    # `styles` row
```

## The remote half

The container that runs the issue/PR agent carries no user-level `~/.claude`,
and the session is opened with `setting_sources=["project"]` on purpose — so it
never sees `~/.claude/output-styles/`. `issue_agent/providers/claude.py` reads
the same file out of this tree instead and appends its body to the
`claude_code` system-prompt preset. One source of truth, two delivery
mechanisms; the Dockerfile copies this tree in beside the wrapper so the file is
there to read.

## Styles

- **terse** — house register: no performative language, no meta-commentary
  about the model's own honesty or effort, corrections stated as corrections,
  uncertainty expressed as a claim about the world. The written-down half of
  what `../rules/working-with-brujoand.md` asks for in prose, moved to the one
  channel that shapes voice before the conversation starts.
