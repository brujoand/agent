---
name: model-routing
description: Choose the Claude model and effort level for a task, and cut token spend in this workspace. Use when picking a model or effort for a subagent, workflow, or `AGENT_MODEL`; when a run felt too expensive or too slow; when deciding whether to escalate after a bad result; or when asked how to reduce Claude Code cost. Not a model-facts reference — pricing, IDs and API limits come from the bundled `claude-api` skill.
---

# Model routing

## Where the facts live

**Do not restate model IDs, prices, context limits, or effort semantics here or
anywhere else in this repo.** The bundled `claude-api` skill ships with the CLI,
updates with it, and is authoritative. A table copied into this repo is a table
that goes stale in weeks and then gets believed. Load `claude-api` for facts;
this skill is only *policy* — the part Anthropic cannot know for us.

The one deliberate exception is `PRICES` in `agentcli/usage.py`, which needs a
frozen snapshot to keep `agent usage` comparable across a price change. It
carries `PRICES_AS_OF` for exactly that reason.

## The two dials

Model and effort are independent, and effort is usually the bigger cost lever.
Anthropic's own rule for which to reach for:

- Claude had the full context, clearly tried, and was still **wrong** → escalate
  the **model**.
- Claude went wrong by **skipping a file, not running tests, or bailing partway**
  → raise the **effort**.

Getting this backwards is the common expensive mistake: raising effort on a model
that lacks the capability just buys more tokens of the same wrong answer.

## Routing

Defaults, not laws. Start here, then measure.

| Work | Start at |
|---|---|
| Interactive engineering (the default) | Opus, `high` |
| Triage, labelling, routing, dedupe | Haiku |
| Estimation, research, codebase exploration | Sonnet, `medium` |
| Planning and architecture | Opus, plan mode first |
| Hard debugging — subtle state, concurrency, timing | Opus, `xhigh`, and only after the obvious causes are ruled out |
| Bulk / offline / non-interactive | Batch API (50% off, stacks with caching) |

Two anti-patterns worth naming:

1. **Don't bump a cheap model to `xhigh` "to be safe."** That is where its price
   advantage is weakest. Escalate the model instead.
2. **Don't reach for `max` reflexively.** Anthropic's own guidance is that it adds
   significant cost for small quality gains on most work, and can overthink
   structured tasks.

## Where the spend actually goes

Not code generation — **context re-reads**. Every turn re-sends the whole
conversation, so a long-lived session pays for its entire history on every
message. In rough order of leverage:

1. `/clear` between unrelated tasks. Costs nothing; `/compact` reads the whole
   context to summarise it.
2. Keep tool output out of the window — filter it, or delegate the verbose
   operation to a subagent so only the summary comes back.
3. Don't switch models or add MCP servers mid-session. Caches are per-model, and
   tools render at the front of the prefix, so either rebuilds the cache from
   zero. (On the newest Opus this is softening — there is now a beta for changing
   tools mid-conversation without invalidating the prefix, and mid-conversation
   `role: "system"` messages that leave the cached prefix intact. Check
   `claude-api` before assuming the old absolute still holds.)
4. Keep `CLAUDE.md` files short. They are input tokens on every single call.

## Verify before you believe

Model-selection writing is full of confident numbers with no checkable source —
benchmark tables, "N× cheaper" claims, per-developer-per-day cost averages. Most
of it cannot be traced to anything Anthropic publishes.

Two rules:

- If a claim is about the API surface — pricing, limits, parameters, defaults —
  check it against `claude-api` before acting on it.
- If it is about *cost or quality on our workload*, measure it. `agent usage`
  reads the real transcripts. Take a baseline, change one dial, compare. A number
  from a blog post is not evidence about this workspace.

## Bumping a model

A version bump is never just the ID string. Each release re-tunes behaviour, and
the guidance written for the previous model can become actively wrong — a
"delegate exploration to subagents" nudge written for a model that under-delegated
becomes a cost multiplier on one that over-delegates. Read the target model's
section of the `claude-api` migration guide, and re-tune the prompt or playbook in
the same change.
