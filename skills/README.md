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

Empty right now. The machinery stays because the next genuinely on-demand skill
belongs here, not in memory.
