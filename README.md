# agent

The root of all agentic tasks, and the `agent` CLI that runs them.

Every repo the `brujoand-agent` GitHub App can reach is cloned as a sibling
directory here (and gitignored). `gitops-homelab` is one of them, which is why
this repo cannot depend on `lab`: it is what puts `lab` on disk.

**The dependency points one way: `agent` → `lab`, never back.**

## Bootstrap

```bash
curl -fsSL https://mise.run | sh     # mise depends on nothing
git clone https://github.com/brujoand/agent ~/src/agent
~/src/agent/bootstrap.sh             # mise install + uv sync + symlink ~/.local/bin/agent
agent pull                           # clone every reachable repo as a sibling
agent lab install                    # install lab from ./gitops-homelab
```

`agent` needs the App credentials in `~/.bash_private` (`APP_ID`,
`APP_INSTALLATION_ID`, `LAB_GH_APP_PRIVATE_KEY`). A human with 1Password access
places them out-of-band with `lab agent bootstrap`. That file is the entire
contract between `lab` and `agent` — never a code path. There is no 1Password
fallback: this host has no `OP_SERVICE_ACCOUNT_TOKEN` and never will.

## Commands

| Command | Purpose |
|---|---|
| `agent doctor` | creds, token, reachable repos, lab, credential helpers |
| `agent github token [--refresh]` | short-lived App installation token |
| `agent git-credential get` | git credential helper (wired into every clone) |
| `agent repos` | HTTPS clone URLs the App installation can reach |
| `agent pull` | clone or fast-forward every reachable repo |
| `agent lab install` | install `lab` from `./gitops-homelab` |
| `agent lab <args...>` | run `lab` with `GH_TOKEN`/`KUBECONFIG` set |
| `agent workspace create <type>/<slug> [--repo]` | worktree off a fresh `origin/main` |
| `agent workspace delete\|list\|gc [--repo]` | manage session worktrees |

Worktrees land at `~/worktrees/<repo>/session-<slug>`; `--repo` defaults to
`gitops-homelab`.

## Authentication

github.com is reached over HTTPS as the App, everywhere — there is no SSH key
and no deploy key. Every clone gets

```
credential.https://github.com.helper = !~/.local/bin/agent git-credential
```

which mints a fresh installation token on demand (cached until shortly before it
expires).

It points at the **installed** agent — an isolated copy that `bootstrap.sh` puts
in `~/.local/bin` with `uv tool install` — never at `./agent` in the checkout.
This CLI is its own credential helper for the repo that contains it, so a helper
living in tracked files is a bootstrap trap: `git checkout` to any commit
predating the CLI deletes both the launcher and `agentcli/`, git then has no way
to authenticate, and `git pull` cannot fetch the commits that would restore it.
(If you ever land there: `git merge --ff-only origin/main` needs no auth.)

The checkout keeps an editable `.venv`, so `./agent` runs live source for
development while `agent` on PATH stays stable. Note `credential.helper` is **multi-valued**: to test one in isolation
you must first reset the list with `-c credential.helper=`, or the helper already
in `.git/config` will quietly authenticate for you. There is no ambient
credential store here, so `agent doctor` treats a stale helper as a failure.

## Development

```bash
mise exec -- uv run pytest
mise exec -- uv run ruff check . && mise exec -- uv run ruff format --check .
```

The App-token mint is implemented twice on purpose: here in `agentcli/github.py`,
and in `gitops-homelab:containers/github-runner/mint-app-token.sh`, which is
baked into the CI runner image. Neither can call the other. Keep the `iat`
backdate, the `exp` window, and the retry/fast-fail classification in sync.
