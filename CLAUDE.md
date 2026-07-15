# agent

The `agent` CLI (Python/Typer): GitHub App tokens, repo sync, session worktrees.
`agent pull` is what clones every other repo in `~/src`.

## Hard rules

- **Never push to `main`/`master`.** Feature branch + PR, always. Open the PR,
  report the URL, stop — **only the human merges.**
- **Conventional Commits** (`feat:`, `fix:`, `chore:`, …). Never hand-bump a version.
- **`pre-commit` is the gate.** Run `pre-commit run --files <changed>` before
  declaring a change done, and report the result.
- **`mise` provisions the toolchain** (`mise install`). New worktrees need `mise trust`.
- Plan every non-trivial task. If the plan fails, restart planning.

## Workflow

Default branch is `main`. Commit types drive nothing automated here (no release
job), but the convention holds.

## Architecture

`agentcli/` is the CLI; `cli.py` wires the Typer app. One module per concern:
`github.py` (App-token mint), `credential.py` (git credential helper),
`workspace.py` (session worktrees), `pull.py` + `repos.py` (repo sync),
`install.py`, `doctor.py`, `creds.py`/`labpass.py`, `config.py`, `errors.py`.

`issue_agent/` is the interactive issue/PR agent, run as a flat script in its
own `/opt/issue-agent` venv: `agent.py` (the wrapper: ASK/DONE marker flow,
GitHub polling, usage metrics) and `providers/` (the LLM seam — `base.py`
protocol + neutral types, `claude.py` Claude Agent SDK adapter, env-driven
factory via `AGENT_PROVIDER`, default `claude`). Both are ours: lint-covered
and tested from `tests/` (a `conftest.py` shim puts `issue_agent/` on
`sys.path`, and `claude-agent-sdk` is a dev dep for the adapter tests). Only
`s3_session_store.py` remains **vendored verbatim** from the Claude Agent SDK
examples and excluded from ruff.

`bootstrap.sh` installs an isolated copy into `~/.local/bin` via `uv tool install`.

## Commands

```bash
mise exec -- uv run pytest                       # all tests (testpaths = tests/)
mise exec -- uv run pytest tests/test_github.py::test_name
mise exec -- uv run ruff check .
mise exec -- uv run ruff format --check .
pre-commit run --files <changed files>
./agent <cmd>          # live source; `agent` on PATH is the stable installed copy
```

## Gotchas

- **`agent` → `lab`, never back.** This repo cannot depend on `lab`, because
  `agent pull` is what puts `lab` on disk in the first place.
- **`agent` is its own git credential helper.** The helper must point at the
  *installed* copy in `~/.local/bin`, never `./agent` in the checkout — a helper
  living in tracked files is a bootstrap trap (checking out a commit predating it
  deletes the helper, and git can then no longer authenticate to restore it).
  `credential.helper` is multi-valued: to test one in isolation, reset first with
  `-c credential.helper=`. `git merge --ff-only origin/main` needs no auth.
- **The App-token mint is implemented twice on purpose** — here in `agentcli/github.py`
  and in `gitops-homelab:containers/github-runner/mint-app-token.sh`. Neither can
  call the other. Keep the `iat` backdate, `exp` window, and retry classification
  in sync.
- **Python is pinned to 3.12** to match the CI runner image, which bakes in
  `agentcli`. The `python-min-version` pre-commit hook parses `agentcli/` and
  `issue_agent/` under 3.12 to catch 3.13+ syntax.
- **`claude-agent-sdk` is pinned twice on purpose** — `issue_agent/requirements.txt`
  (the runtime venv) and the pyproject dev-dependencies (so tests import real SDK
  types). Keep the pins in sync; both carry a comment saying so.
- This repo handles a private key. `gitleaks` runs in pre-commit; keep it that way.
