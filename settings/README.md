# Managed Claude settings

`settings.json` here is Claude Code's own settings schema. `agent settings
install` merges the keys it declares into `~/.claude/settings.json`; `agent
install` runs it alongside skills, rules, hooks, and output styles.

This is the last of the five ways the repo ships behaviour, and the only one that
ships **policy** rather than code:

- a **skill** ([`../skills/`](../skills)) is opt-in — Claude decides whether to
  load it;
- a **rule** ([`../rules/`](../rules)) is always on — in context before the first
  response;
- a **hook** ([`../hooks/`](../hooks)) is not context at all — the harness running
  a command when something happens;
- an **output style** ([`../output-styles/`](../output-styles)) is spliced into
  the system prompt — register, before the conversation starts;
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
| `outputStyle` | `terse` | Selects [`../output-styles/terse.md`](../output-styles/terse.md). The tree ships every style; this key is what makes one active, so a style is never on until it is declared here. Matched on the style's frontmatter `name`, which defaults to the filename — [`tests/test_output_styles.py`](../tests/test_output_styles.py) asserts the key, the filename and the `name` still agree. |
| `autoCompactEnabled` | `true` | Explicit, because `autoCompactWindow` means nothing without it. |
| `autoCompactWindow` | `450000` | The big one, and the one to revisit. On a 1M-context model nothing forces a reset, so sessions coast at 400–900k and every turn re-reads all of it: `agent usage` measured turns above 200k context at 45% of turns but **76% of all re-read tokens**, roughly half of total spend. So there is a cap. Where it sits is a trade, not a measurement — lower means cheaper turns and more compactions, higher means fewer interruptions and more re-read tokens per turn. Raised from `250000` in July 2026, and the reason is the half of the trade the token figures do not show: at 250k, real sessions compacted *mid-task*, and a compaction landing before the work is done costs the summary **and** the re-read, having also dropped detail the task still needed. It is also the budget [`hooks/context-budget.sh`](../hooks/context-budget.sh) warns against (at 60% and 85%, so now 270k and 382k), so the two move together by construction. |
| `env.*` (telemetry) | see below | Claude Code's OpenTelemetry exporter, pointed at the workspace's Prometheus OTLP receiver. |

## Telemetry

The exporter block was hand-written into each workstation's `settings.json` until
2026-07-26, and drifted exactly as unmanaged policy does: one workstation
reported, the other never had the block at all, and the gap went unnoticed for a
month because a missing exporter and a quiet week look identical downstream. It
is declared here now so that "is telemetry on?" is answered by `agent settings
list` on any host instead of by reading a file on that host.

Three values are host-supplied, so nothing identifying the infrastructure is in
this repo. Set them where the rest of this host's agent environment lives —
`~/.bash_private`:

| variable | example | notes |
|---|---|---|
| `AGENT_TELEMETRY_ENABLED` | `1` | The master switch, gated on purpose. Without it `CLAUDE_CODE_ENABLE_TELEMETRY` is skipped, so a host that supplies no endpoint does not get an exporter pointed at the OTel default of `localhost:4318`, retrying forever into nothing. |
| `AGENT_OTLP_ENDPOINT` | `https://prometheus.example.com/api/v1/otlp` | The receiver's base URL. The write path must be exempt from any OIDC in front of it — OTLP exporters cannot do an interactive auth flow. |
| `AGENT_TELEMETRY_HOST` | `workstation-1` | Becomes `host=` in `OTEL_RESOURCE_ATTRIBUTES`; how one workstation is told from another in a query. |

Set all three or none. A partial set is visible in one command — `agent settings
list` prints every skipped key and why — which is the property the old
hand-maintained block did not have.

Two fixed values are load-bearing and easy to lose if anyone re-types this block
by hand:

- `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative` — Prometheus's
  OTLP receiver rejects delta temporality, which is the exporter's default. It
  answers HTTP 500 and the client logs nothing useful, so the symptom is silence.
- `OTEL_METRICS_INCLUDE_ENTRYPOINT=true` — supplies `app_entrypoint`, which is
  the only thing separating an interactive session from CI in the dashboard.

Takes effect for **new** sessions. Verify from a host that can read Prometheus:

```bash
lab prometheus query \
  'sum by (host, app_entrypoint) (last_over_time(claude_code_session_count_total[7d]))'
```

Use a range selector, never a bare instant query — an instant query only sees
series with a sample in the last five minutes, so it returns nothing once a
session ends and reads as "telemetry is broken" when it is working.

`hooks` is refused here on purpose: [`../hooks/hooks.json`](../hooks/hooks.json)
owns that key, and two installers writing one key would flap. The refusal happens
when the declaration loads, so it fails on the PR that introduces it rather than
quietly on someone's machine.
