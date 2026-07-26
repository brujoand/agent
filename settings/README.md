# Managed Claude settings

`settings.json` here is Claude Code's own settings schema. `agent settings
install` merges the keys it declares into `~/.claude/settings.json`; `agent
install` runs it alongside skills, rules, and hooks.

This is the fourth way the repo ships behaviour, and the only one that ships
**policy** rather than code:

- a **skill** ([`../skills/`](../skills)) is opt-in — Claude decides whether to
  load it;
- a **rule** ([`../rules/`](../rules)) is always on — in context before the first
  response;
- a **hook** ([`../hooks/`](../hooks)) is not context at all — the harness running
  a command when something happens;
- a **setting** is not context either, and not a command: it is a knob on the
  harness itself. `effortLevel` and `autoCompactWindow` are cost controls, and a
  cost control you have to remember to re-apply on each host is not a control.

## Run it as often as you like

Three properties, and the whole design follows from wanting all three at once:

**Narrow.** Only declared keys are touched. Everything else in your settings
survives byte-for-byte — including the values that *cannot* live in a public repo,
like a private OTLP endpoint or personal permission rules.

**Convergent in both directions.** The keys we applied are recorded at
`state/agent-managed-settings.json` under the config root. Delete a key from the
declaration and the next install deletes it from your settings. Without that
record the mechanism could only ever add, and a retired setting would linger on
every host that ever saw it. If the record is lost we simply stop pruning: the
failure mode is a stale key left behind, never a key deleted on a guess.

**Idempotent.** A second run reports `ok` for every key and rewrites nothing — the
file is not even touched, because Claude Code watches it and a rewrite for nothing
is a reload for nothing.

```bash
agent settings list       # what is declared, and whether it is applied
agent settings install    # converge; safe to re-run forever
agent doctor              # reports drift as a failed check
```

## Host-specific values: `${VAR}`

A string value may reference the environment. This repo is public, so a value that
identifies internal infrastructure must not appear in it — declare the *shape*
here and let the host supply the value:

```json
{
  "env": {
    "OTEL_EXPORTER_OTLP_ENDPOINT": "${AGENT_OTLP_ENDPOINT}"
  }
}
```

If the variable is unset the key is **skipped and reported** — never written with
the placeholder left in. A literal `${AGENT_OTLP_ENDPOINT}` in `settings.json`
would misconfigure telemetry silently, which is worse than not configuring it at
all. Only `${VAR}` is recognised; bare `$VAR` is deliberately not expanded,
because settings values contain shell-ish strings that were never meant as
references.

## What is declared, and why

| key | value | why |
|---|---|---|
| `effortLevel` | `high` | Claude Code defaults to `xhigh`. Measured on this host, ~97% of output tokens are thinking — about 17% of total spend — and `high` is the documented recommended minimum for intelligence-sensitive work. Drop to `medium` per session for mechanical passes with `/effort`. |
| `autoCompactEnabled` | `true` | Explicit, because `autoCompactWindow` means nothing without it. |
| `autoCompactWindow` | `250000` | The big one. `agent usage` measured turns above 200k context at 45% of turns but **76% of all re-read tokens** — roughly half of total spend. On a 1M-context model nothing forces a reset, so sessions coast at 400–900k and every turn re-reads all of it. This caps the effective window. It is also the budget [`hooks/context-budget.sh`](../hooks/context-budget.sh) warns against, so the two agree by construction. |

`hooks` is refused here on purpose: [`../hooks/hooks.json`](../hooks/hooks.json)
owns that key, and two installers writing one key would flap. The refusal happens
when the declaration loads, so it fails on the PR that introduces it rather than
quietly on someone's machine.
