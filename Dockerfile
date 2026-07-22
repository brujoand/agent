FROM ghcr.io/actions/actions-runner:latest

# The self-hosted runner image for the agent workflows. Built from THIS repo so
# it can bake in the `agent` CLI: the App-token mint has to exist before
# `actions/checkout` runs, and the CLI is what mints.
#
# Base utilities our workflows need beyond the stock runner image. Project tool
# versions (gh, kubectl, yq, pre-commit, etc.) are provided at job time via mise
# + the consuming repo's mise.toml; this image ships mise + base deps, the
# interactive issue-agent runtime (Claude Code CLI + Python SDK wrapper), and the
# agent CLI.
USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl \
      git \
      ca-certificates \
      gnupg \
      jq \
      unzip \
      python3 \
      python3-venv \
      nodejs \
      npm \
 && rm -rf /var/lib/apt/lists/*

# GitHub CLI (`gh`) — the issue-agent runtime shells out to it for every read and
# write (view/comment on issues + PRs, open a PR). It used to come from the
# consuming repo's mise.toml at job time, but the central hub runs against repos
# that don't provide it (and its scan runs with no repo checked out at all), so
# bake it into the image where the runtime can always find it.
RUN mkdir -p -m 755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/* \
 && gh --version

# mise: activated at job time to install the repo's pinned toolchain.
RUN curl -fsSL https://mise.run | MISE_INSTALL_PATH=/usr/local/bin/mise sh \
 && /usr/local/bin/mise --version

# Claude Code CLI — the Agent SDK shells out to it. Pinned; the action installed
# it itself, but the SDK wrapper needs it baked in.
RUN npm install -g @anthropic-ai/claude-code@2.1.195 \
 && npm cache clean --force \
 && claude --version

# Interactive issue-agent: a venv with the Agent SDK + boto3, and the wrapper
# source copied to /opt/issue-agent. The issue-agent workflow runs
# `/opt/issue-agent/venv/bin/python /opt/issue-agent/agent.py`.
COPY issue_agent/ /opt/issue-agent/
RUN python3 -m venv /opt/issue-agent/venv \
 && /opt/issue-agent/venv/bin/pip install --no-cache-dir \
      -r /opt/issue-agent/requirements.txt \
 && /opt/issue-agent/venv/bin/python -c "import sys; sys.path.insert(0, '/opt/issue-agent'); import agent, providers.claude, boto3"

# The agent CLI. Baked in so it exists BEFORE `actions/checkout` runs -- the
# checkout token is what it mints. That ordering is the whole reason the mint
# used to be duplicated as a standalone bash script; now the image is built from
# the repo that owns the CLI, so there is one implementation.
#
# The venv lands at /opt/agent/.venv so the repo's own `agent` launcher works
# unmodified: it resolves `.venv` relative to its own `readlink -f` path. No
# container-specific entrypoint to keep in sync.
#
# python3 here is Ubuntu 24.04's 3.12, which is exactly why pyproject.toml floors
# at 3.12 and ruff targets py312.
COPY pyproject.toml README.md agent /opt/agent/
COPY agentcli/ /opt/agent/agentcli/
RUN python3 -m venv /opt/agent/.venv \
 && /opt/agent/.venv/bin/pip install --no-cache-dir /opt/agent \
 && ln -s /opt/agent/agent /usr/local/bin/agent \
 && /opt/agent/.venv/bin/python -c "import agentcli.github, agentcli.creds"

USER runner
