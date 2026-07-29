#!/usr/bin/env bash
# Warn when a session's resident context has grown expensive.
#
# Installed and wired by `agent hooks install` (declaration: hooks/hooks.json):
#   UserPromptSubmit -> checks the session's own transcript before each prompt
#
# Why this exists. Every turn re-sends the whole conversation, so a session's
# cost is the sum of its context size over its turns -- quadratic in turn count,
# not linear. Measured on this host with `agent usage`: turns above 200k context
# were 45% of turns but 76% of all re-read tokens, or about half of total spend.
# The fix is to start a new session at a task boundary, and the only thing that
# makes that happen reliably is being told, at the moment it matters.
#
# `/clear` is free. Auto-compaction is not: it reads the entire context to write
# a summary, and it drops detail. So this warns *before* the compaction window,
# to make the free option the one taken.
#
# The budget is `autoCompactWindow` from settings.json (the point where Claude
# Code compacts on its own), so there is one number to tune, not two.
# AGENT_CONTEXT_BUDGET overrides it for a session.
#
# Reads the hook payload on stdin. Warnings go out as a `systemMessage`, which
# reaches the human only. Nothing else may reach stdout: UserPromptSubmit stdout
# is injected into the model's context, so a chattier version of this hook would
# spend tokens to warn about spending tokens.
#
# Fires at most once per threshold per session -- a warning on every prompt is a
# warning that gets ignored.
#
# Test seam: `context-budget.sh --print < payload.json` prints `<level> <tokens>
# <budget>` and touches no state.
set -e

# Only consulted when settings.json has no autoCompactWindow. Kept equal to the
# window this workspace ships (settings/settings.json) so a host that has not run
# `agent settings install` is warned against the number it is about to get.
readonly DEFAULT_BUDGET=450000

# Fractions of the budget that trigger each level. `warn` is early enough that
# clearing still saves a meaningful number of turns; `high` means compaction is
# close, so the choice is now clear-or-pay.
readonly WARN_FRACTION=60
readonly HIGH_FRACTION=85

# How far back to look for the newest usage record. Every assistant turn writes
# one, so this is generous; bounding it keeps the hook off the critical path of a
# transcript that can reach hundreds of megabytes.
readonly TAIL_LINES=400

budget::resolve() {
  local settings value
  if [[ -n ${AGENT_CONTEXT_BUDGET:-} ]]; then
    printf '%s' "$AGENT_CONTEXT_BUDGET"
    return 0
  fi
  settings="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
  value=""
  if [[ -f $settings ]]; then
    value="$(jq -r '.autoCompactWindow // empty' "$settings" 2>/dev/null || true)"
  fi
  if [[ $value =~ ^[0-9]+$ ]] && [[ $value -gt 0 ]]; then
    printf '%s' "$value"
  else
    printf '%s' "$DEFAULT_BUDGET"
  fi
}

# Resident context on the most recent billed turn: fresh input plus both cache
# halves. That sum is what the next turn re-sends, which is what it costs.
context::resident() {
  local path="$1"
  local line total
  if [[ -z $path || ! -f $path ]]; then
    printf '0'
    return 0
  fi
  line="$(tail -n "$TAIL_LINES" "$path" 2>/dev/null | grep '"usage"' | tail -n 1 || true)"
  if [[ -z $line ]]; then
    printf '0'
    return 0
  fi
  total="$(jq -r '
    (.message.usage // {}) |
    ((.input_tokens // 0) + (.cache_creation_input_tokens // 0) + (.cache_read_input_tokens // 0))
  ' <<<"$line" 2>/dev/null || true)"
  if [[ $total =~ ^[0-9]+$ ]]; then
    printf '%s' "$total"
  else
    printf '0'
  fi
}

context::level() {
  local tokens="$1" budget="$2"
  local percent
  if [[ $budget -le 0 ]]; then
    printf 'none'
    return 0
  fi
  percent=$((tokens * 100 / budget))
  if [[ $percent -ge $HIGH_FRACTION ]]; then
    printf 'high'
  elif [[ $percent -ge $WARN_FRACTION ]]; then
    printf 'warn'
  else
    printf 'none'
  fi
}

context::message() {
  local level="$1" tokens="$2" budget="$3"
  local percent
  percent=$((tokens * 100 / budget))
  # Lead with the action. The re-read figure is the part that makes the cost
  # concrete: it is what every remaining turn in this session pays again.
  if [[ $level == "high" ]]; then
    printf '/clear now if this task is done -- context is %dk of %dk (%d%%), so compaction is close. Every further turn re-reads %dk tokens. Compaction reads all of it to write a summary and loses detail; /clear costs nothing.' \
      $((tokens / 1000)) $((budget / 1000)) "$percent" $((tokens / 1000))
  else
    printf 'Consider /clear if you are starting something new -- context is %dk of %dk (%d%%), and every further turn re-reads %dk tokens. Clearing at a task boundary is free; compaction at %dk is not.' \
      $((tokens / 1000)) $((budget / 1000)) "$percent" $((tokens / 1000)) $((budget / 1000))
  fi
}

main() {
  local print_only="no"
  if [[ ${1:-} == "--print" ]]; then
    print_only="yes"
  fi

  local payload transcript session budget tokens level
  payload="$(cat)"
  transcript="$(jq -r '.transcript_path // empty' <<<"$payload" 2>/dev/null || true)"
  session="$(jq -r '.session_id // "unknown"' <<<"$payload" 2>/dev/null || true)"

  budget="$(budget::resolve)"
  tokens="$(context::resident "$transcript")"
  level="$(context::level "$tokens" "$budget")"

  if [[ $print_only == "yes" ]]; then
    printf '%s %s %s\n' "$level" "$tokens" "$budget"
    exit 0
  fi

  if [[ $level == "none" ]]; then
    exit 0
  fi

  # Same config root the installer uses, so a moved CLAUDE_CONFIG_DIR takes the
  # once-per-threshold stamps with it.
  local state_dir stamp
  state_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/state/context-budget"
  stamp="${state_dir}/${session}.${level}"
  if [[ -e $stamp ]]; then
    exit 0
  fi

  jq -n --arg msg "$(context::message "$level" "$tokens" "$budget")" '{systemMessage: $msg}'

  mkdir -p "$state_dir"
  : >"$stamp"
  find "$state_dir" -type f -mtime +7 -delete 2>/dev/null || true
}

main "$@"
