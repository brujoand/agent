# Shared Claude skills

One directory per skill, each holding a `SKILL.md`. `agent skills install`
symlinks them into `~/.claude/skills/`. Loose files here (this one included) are
ignored.

Skills are **opt-in**: Claude sees a skill's name and description and decides
whether to load the body. That fits a task procedure — something needed only
when the task matches, and too expensive to carry otherwise.

Always-on house style is the opposite case and lives in [`../rules/`](../rules),
imported into `~/.claude/CLAUDE.md` by `agent rules install`. `working-with-brujoand`
started here and moved there for exactly that reason: as a skill it was merely
*available*, and a session that never invoked it never followed it.

## Skills

- [`model-routing`](model-routing) — which model and effort level to use, and
  where the token spend actually goes. The routing *policy*; the model facts it
  defers to live in the CLI's bundled `claude-api` skill, deliberately not copied
  here. Earns its place as a skill rather than a rule because it applies when
  choosing a model or chasing a cost, not on every turn.
