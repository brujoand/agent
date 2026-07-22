# PRINCIPLES.md — how we work with Claude across the workspace

The standard this workspace holds Claude to. It generalizes across every repo
here; the `working-with-brujoand` skill (in `skills/`) is its response-shaping
slice, distributed to every repo by `agent skills install`.

## Non-negotiables

- **No hype adoption.** Do not add a tool because it is popular or trending.
  Popularity is not evidence. Every new tool, dependency, or MCP server clears
  the selection criteria below, or it stays out.
- **Prefer what's already here.** A new external dependency (MCP server, indexer,
  proxy) is a cost to justify, not a default. Built-ins win ties.
- **Deterministic beats probabilistic.** If a check can be a command, it belongs
  in a hook or CI gate, not in the model's reasoning turns.

## The principles

1. **Deterministic-first, LLM-last.** Any failure mode a validator can catch gets
   caught deterministically before you reach for an AI-side mitigation. Tokens
   spent making the model re-derive what a linter would catch are pure waste.
2. **Every tool pays context rent.** A tool's schema costs context on every
   request, used or not. It earns its slot only when
   `frequency-of-use × value-per-use > standing schema cost`. Prefer lazy tool
   discovery over eagerly mounting large tool sets; audit the active tool list
   biased toward removal.
3. **Lean context beats large context.** Grep to the relevant lines, then read
   those lines — not whole files. Use subagents to isolate specialized work so
   the main thread stays small. Keep CLAUDE.md files minimal; every line must
   earn permanent residence in context.
4. **Caching is structural.** Order context by stability: stable, high-value
   content above the cache breakpoint, volatile content below. Maximize the
   cache-read ratio; treat a low cache-hit rate as a bug to investigate.
5. **Right-size the model per task.** Cheap model for mechanical/triage work,
   mid-tier as the default, top-tier reserved for genuinely hard reasoning.
   Never a default-to-biggest reflex.
6. **Measure before and after (advisory).** Prefer changes that move a real
   metric — token usage by type, cache-hit ratio, turns-per-task, task success.
   A change that moves nothing and adds no clear accuracy gain should not ship.
7. **Plan every non-trivial task.** State what changes, in what order, and how it
   is verified. If the plan fails partway, restart planning rather than patch a
   broken plan step by step.

## Selection criteria — what any addition must pass

Before adding any tool, MCP server, or technique, it must pass all of:

1. **Named failure or cost** it addresses. "General improvement" does not qualify.
2. **No built-in already covers it** — ripgrep, git, existing CLIs, hooks, and
   subagents have been ruled out for this specific need.
3. **Net-positive context economics** — its standing cost (schema, prompt) is
   smaller than its expected savings.
4. **Measurable** — its effect will show up in real metrics.
5. **Bounded surface** — its maintenance, auth, and failure surface is acceptable.

Fail any item → don't add it, or add it behind a flag and let the metrics decide.

## Quick heuristics

- Can a command catch it? → hook or CI gate, not the model.
- Do we already have a tool for it? → use it, don't add one.
- Does a tool sit in context unused? → remove it.
- Am I reading a whole file? → grep first, then read the relevant span.
- Is this content stable? → above the cache breakpoint. Volatile? → below it.
- Is this the cheapest model that does the job? → if not, justify the upgrade.
