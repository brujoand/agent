# Two images, one file, two `--target`s:
#
#   runner  the CI image for the agent workflows (FROM actions-runner)
#   agent   the interactive image `bagent` launches on a workstation
#
# They do NOT share a base layer, and that is a constraint rather than an
# oversight: the CI image must be `FROM actions-runner` (it needs the runner
# binaries and its entrypoint), while the interactive image must not carry any
# of that. Two different roots cannot share layers. What they DO share is every
# pinned version, declared once as global ARGs below -- which is where drift
# would actually hurt, since a Claude CLI that differs between the two would
# make a bug reproduce in one and not the other.
#
# Rebasing CI onto a common root is possible but would rewrite how the
# self-hosted runners start, so it is deliberately not bundled here.
#
# NOTE: with no `--target`, docker builds the LAST stage (agent). The workflow
# names both targets explicitly.

# Shared pins. Re-declared inside each stage that uses them (an ARG before the
# first FROM is global but not automatically in scope).
ARG CLAUDE_CODE_VERSION=2.1.195

# The interactive image's agent account. uid/gid are build args because
# /mnt/bagent and /mnt/src are host mounts: the ownership on the host has to
# match the account in the image, and only the deployment knows what that is.
ARG AGENT_LOGIN=brujoand-agent
ARG AGENT_UID=2000
ARG AGENT_GID=2000

FROM ghcr.io/actions/actions-runner:latest AS runner

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
ARG CLAUDE_CODE_VERSION
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && claude --version

# Interactive issue-agent: a venv with the Agent SDK + boto3, and the wrapper
# source copied to /opt/issue-agent. The issue-agent workflow runs
# `/opt/issue-agent/venv/bin/python /opt/issue-agent/agent.py`.
COPY issue_agent/ /opt/issue-agent/
# The output styles the interactive setup symlinks into ~/.claude/output-styles/.
# This runtime has no user-level ~/.claude and loads project settings only, so
# providers/claude.py reads the style out of this tree and appends it to the
# system-prompt preset instead. Same file, same PR, both ways of shipping it.
COPY output-styles/ /opt/issue-agent/output-styles/
RUN python3 -m venv /opt/issue-agent/venv \
 && /opt/issue-agent/venv/bin/pip install --no-cache-dir \
      -r /opt/issue-agent/requirements.txt \
 && /opt/issue-agent/venv/bin/python -c "import sys; sys.path.insert(0, '/opt/issue-agent'); import agent, providers.claude, boto3" \
 && test -f /opt/issue-agent/output-styles/terse.md

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

# =============================================================================
# The interactive image: what `bagent` launches on a workstation.
#
# Deliberately NOT the runner image. It carries no Actions runner, no
# issue-agent runtime (that is the CI path), and nothing else a human sitting in
# a shell does not need -- the point of moving off a dedicated unix account was
# to shrink what the agent can reach, and a smaller image is part of that.
#
# It sees exactly two paths at runtime, both host mounts:
#   /mnt/src      the checkouts, laid out <owner>/<repo>
#   /mnt/bagent   HOME -- App credentials, ~/.claude, caches
#
# HOME being a mount is what lets the image stay disposable while the agent's
# state survives: rebuild and the next session is current.
#
# Ubuntu 24.04 for python3.12, matching .python-version and the floor in
# pyproject.toml -- the same interpreter the runner image resolves to.
# =============================================================================
FROM ubuntu:24.04 AS agent

ARG CLAUDE_CODE_VERSION
ARG AGENT_LOGIN
ARG AGENT_UID
ARG AGENT_GID

ENV DEBIAN_FRONTEND=noninteractive

# `openssh-client` earns its place: the `--with ssh` profile lends the human's
# key, which is useless without a client. `less` because git paginates.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      gnupg \
      jq \
      less \
      nodejs \
      npm \
      openssh-client \
      python3 \
      python3-venv \
      unzip \
 && rm -rf /var/lib/apt/lists/*

# GitHub CLI: the agent reads and writes issues and PRs through it constantly.
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

# mise provisions each repo's pinned toolchain at session time, exactly as it
# does on a workstation.
RUN curl -fsSL https://mise.run | MISE_INSTALL_PATH=/usr/local/bin/mise sh \
 && /usr/local/bin/mise --version

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && claude --version

# The agent CLI, same layout as the runner stage so `/opt/agent/agent` resolves
# its own .venv unmodified.
COPY pyproject.toml README.md agent /opt/agent/
COPY agentcli/ /opt/agent/agentcli/
RUN python3 -m venv /opt/agent/.venv \
 && /opt/agent/.venv/bin/pip install --no-cache-dir /opt/agent \
 && ln -s /opt/agent/agent /usr/local/bin/agent \
 && /opt/agent/.venv/bin/python -c "import agentcli.github, agentcli.creds"

COPY containers/agent-entrypoint.sh /usr/local/bin/agent-entrypoint
RUN chmod +x /usr/local/bin/agent-entrypoint

# The account the agent runs as. uid/gid must match the ownership of the host
# mounts, which is why they are build args rather than whatever adduser picks.
RUN groupadd --gid "${AGENT_GID}" "${AGENT_LOGIN}" \
 && useradd --uid "${AGENT_UID}" --gid "${AGENT_GID}" --shell /bin/bash \
      --home-dir /mnt/bagent --no-create-home "${AGENT_LOGIN}" \
 && mkdir -p /mnt/src /mnt/bagent \
 && chown "${AGENT_UID}:${AGENT_GID}" /mnt/bagent

# Defaults, not requirements: `bagent` passes both explicitly, so the image
# still behaves if it is ever run by hand.
ENV HOME=/mnt/bagent \
    AGENT_SRC_ROOT=/mnt/src \
    AGENT_USER=${AGENT_LOGIN}

USER ${AGENT_UID}:${AGENT_GID}
WORKDIR /mnt/src

ENTRYPOINT ["/usr/local/bin/agent-entrypoint"]
CMD ["claude"]
