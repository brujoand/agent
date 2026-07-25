#!/usr/bin/env bash
# Rename the tmux window to a short camelCase summary of the current task.
#
# Installed and wired by `agent hooks install` (declaration: hooks/hooks.json):
#   UserPromptSubmit          -> titles from the FIRST prompt of a session only
#   PostToolUse/ExitPlanMode  -> re-titles from the accepted plan (always wins)
#
# Titles are sized for a phone-width tab strip: three content words, camelCased,
# 18 chars. Derivation is pure bash over the payload -- no model call, so the
# hook adds no latency to the turn it fires on.
#
# Reads the hook payload on stdin. No-ops outside tmux. Never writes to stdout
# when run as a hook: UserPromptSubmit stdout is injected into the model's
# context.
#
# Test seam: `tmux-title.sh --print < payload.json` prints the derived title
# and touches neither tmux nor the session state.
set -e

readonly TITLE_MAX_WORDS=3
readonly TITLE_MAX_CHARS=18
readonly TITLE_STOPWORDS=" a an and are as at be by can could do does for from get got has have how i if in into is it its just let lets like make me my need needs of on or our out please should so than that the their them then there these they this those to up us use want was we what when where which who why will with would you your "
# Generic plan/prompt section headings that say nothing about the task.
readonly TITLE_FILLER=" approach background changes context goal goals hey implementation objective overview plan please problem proposal steps summary task tasks todo "

# Reduce free text to a short camelCase slug: first few content words, no spaces.
title::slugify() {
  local text="$1"
  local words out word count candidate

  # Hyphens/underscores join (pre-commit -> precommit); everything else splits.
  words="$(printf '%s' "${text:0:400}" |
    tr '[:upper:]' '[:lower:]' |
    sed -e 's#https\?://[^ ]*# #g' -e 's#[-_]##g' -e 's#[^a-z0-9]# #g')"

  out=""
  count=0
  for word in $words; do
    if [[ ${#word} -lt 2 ]]; then
      continue
    fi
    if [[ $TITLE_STOPWORDS == *" $word "* || $TITLE_FILLER == *" $word "* ]]; then
      continue
    fi
    if [[ -z $out ]]; then
      candidate="$word"
    else
      candidate="${out}${word^}"
    fi
    # Drop a word that would overflow rather than cutting it mid-word.
    if [[ ${#candidate} -gt $TITLE_MAX_CHARS ]]; then
      if [[ -n $out ]]; then
        break
      fi
      candidate="${candidate:0:TITLE_MAX_CHARS}"
    fi
    out="$candidate"
    count=$((count + 1))
    if [[ $count -ge $TITLE_MAX_WORDS ]]; then
      break
    fi
  done

  printf '%s' "$out"
}

main() {
  local print_only="no"
  if [[ ${1:-} == "--print" ]]; then
    print_only="yes"
  fi

  local payload text plan stamp state_dir title
  payload="$(cat)"

  # A plan payload always re-titles; a bare prompt titles only once per session.
  plan="$(jq -r '.tool_input.plan // empty' <<<"$payload")"
  # Same config root the installer uses, so a moved CLAUDE_CONFIG_DIR takes the
  # once-per-session stamps with it.
  state_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/state/tmux-title"
  stamp="${state_dir}/$(jq -r '.session_id // "unknown"' <<<"$payload")"

  if [[ -n $plan ]]; then
    text="$plan"
  else
    if [[ $print_only == "no" && -e $stamp ]]; then
      exit 0
    fi
    text="$(jq -r '.prompt // empty' <<<"$payload")"
  fi

  title="$(title::slugify "$text")"

  if [[ $print_only == "yes" ]]; then
    printf '%s\n' "$title"
    exit 0
  fi

  if [[ -z $title || -z ${TMUX:-} ]] || ! command -v tmux >/dev/null 2>&1; then
    exit 0
  fi

  local target="${TMUX_PANE:-}"
  if [[ -z $target ]]; then
    target="$(tmux display-message -p '#{pane_id}' 2>/dev/null || true)"
  fi
  if [[ -z $target ]]; then
    exit 0
  fi

  tmux set-window-option -t "$target" automatic-rename off >/dev/null 2>&1 || true
  tmux rename-window -t "$target" "$title" >/dev/null 2>&1 || true

  mkdir -p "$state_dir"
  : >"$stamp"
  find "$state_dir" -type f -mtime +7 -delete 2>/dev/null || true
}

main "$@"
