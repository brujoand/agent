# Triage-and-fix playbook (generic default)

This is the fallback playbook the issue agent follows when the target repo ships
no playbook of its own at `.claude/commands/triage-and-fix.md`. A repo that wants
tailored behaviour overrides this by committing its own file at that path (it is
loaded automatically because the session loads the repo's `.claude/`).

You are running non-interactively in CI on one issue. Read the repo's `CLAUDE.md`
first — it is the source of truth for conventions, tooling, and guardrails, and
overrides anything here that conflicts.

## 1. Understand the issue

- Read it in full, including the whole comment thread:
  `gh issue view <n> --repo <owner/repo> --comments`.
- Investigate the codebase before deciding anything. Delegate read-heavy
  exploration to a subagent via the `Task` tool where the repo defines one.
- Ask a clarifying question (via the `<<<ASK>>>` markers) ONLY for things you
  genuinely cannot determine by reading the repo — intent, a desired behaviour,
  a real fork between valid approaches, or facts not observable in the code. Ask
  all open questions at once, then stop and wait.

## 2. Size the work

- **S / M** — a well-scoped, low-risk change you can make confidently: implement
  it and open a PR (section 3).
- **L / XL** — large, cross-cutting, risky, or ambiguous: do NOT branch or open a
  PR. Post your findings and a proposed approach as the `<<<DONE>>>` summary, and
  apply any triage labels the repo uses (with `gh issue edit`, which is an edit,
  not a comment). When in doubt about size or confidence, treat it as larger and
  gather data rather than committing a speculative fix.

## 3. S / M — implement and open a PR

1. Branch off the default branch: `git switch -c agent/issue-<n>-<slug>`.
2. Make the **minimal** change that resolves the issue. Match the surrounding
   code's style and conventions.
3. Run the repo's checks before committing — e.g. `pre-commit run --files <changed>`
   if the repo uses pre-commit, plus its tests where relevant. Report failures;
   do not claim success on a red check.
4. Commit referencing the issue (`#<n>`), using the repo's commit convention
   (e.g. Conventional Commits if the repo uses them).
5. `git push -u origin HEAD`, then open a ready (non-draft) PR against the default
   branch: `gh pr create --fill --base <default-branch>`. The PR body MUST contain
   `Closes #<n>` so the issue links to it.
6. Report the PR in your `<<<DONE>>>` summary: the link (`#<n>` or the full URL)
   plus a 2-3 line root-cause / what-changed.

## Guardrails (hard rules)

- **NEVER** push to the default branch, **NEVER** force-push, **NEVER** merge a
  PR or enable auto-merge. Every change lands via a feature branch + PR; only the
  human merges.
- **NEVER** delete or overwrite persistent data or destructive-to-reverse state.
- If your confidence is low, or the change touches security-, data-, or
  infrastructure-critical paths, downgrade to L/XL and gather data instead of
  making an autonomous change.
- Do not post `<<<ASK>>>`/`<<<DONE>>>` content yourself with `gh ... comment` —
  the wrapper posts it. Emit the markers and stop.
