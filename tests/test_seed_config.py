"""Seed-prompt genericization and the repo-specific knobs that feed it:
base-branch derivation and playbook resolution. These are what let one runtime
serve any repo, not just gitops-homelab."""

import agent
import pytest


@pytest.mark.parametrize("template", [agent.SEED_PROMPT, agent.PR_SEED_PROMPT])
def test_seed_prompts_carry_no_hardcoded_repo(template):
    # The gitops-specific repo name must not be baked into the shared runtime.
    assert "gitops-homelab" not in template


def test_seed_prompt_formats_repo_base_playbook():
    out = agent.SEED_PROMPT.format(
        issue="42", repo="brujoand/waiting-games", base="trunk", playbook="PLAYBOOK.md"
    )
    assert "brujoand/waiting-games" in out
    assert "gh pr create --base trunk" in out
    assert "`PLAYBOOK.md`" in out
    assert "**#42**" in out


def test_pr_seed_prompt_formats_repo_and_base():
    out = agent.PR_SEED_PROMPT.format(
        pr="7", repo="brujoand/waiting-games", base="trunk", issue_context=""
    )
    assert "brujoand/waiting-games" in out
    assert "NEVER push to `trunk`" in out
    assert "**PR #7**" in out


def test_default_branch_for_env_override_wins(monkeypatch):
    monkeypatch.setenv("AGENT_BASE_BRANCH", "release")
    # gh must not be consulted when the override is set.
    monkeypatch.setattr(agent, "gh", lambda *a, **k: pytest.fail("gh should not run"))
    assert agent.default_branch_for("brujoand/x") == "release"


def test_default_branch_for_queries_gh(monkeypatch):
    monkeypatch.delenv("AGENT_BASE_BRANCH", raising=False)
    monkeypatch.setattr(agent, "gh", lambda *a, **k: "master")
    assert agent.default_branch_for("brujoand/x") == "master"


def test_default_branch_for_falls_back_to_main(monkeypatch):
    monkeypatch.delenv("AGENT_BASE_BRANCH", raising=False)
    monkeypatch.setattr(agent, "gh", lambda *a, **k: "")
    assert agent.default_branch_for("brujoand/x") == "main"


def test_resolve_playbook_prefers_repo_file(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PLAYBOOK", raising=False)
    pb = tmp_path / ".claude" / "commands" / "triage-and-fix.md"
    pb.parent.mkdir(parents=True)
    pb.write_text("repo playbook")
    assert agent.resolve_playbook(str(tmp_path)) == ".claude/commands/triage-and-fix.md"


def test_resolve_playbook_falls_back_to_baked_default(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PLAYBOOK", raising=False)
    # Empty checkout -> the generic default shipped beside the wrapper.
    assert agent.resolve_playbook(str(tmp_path)) == agent.DEFAULT_PLAYBOOK


def test_resolve_playbook_honours_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PLAYBOOK", "docs/agent.md")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "agent.md").write_text("custom")
    assert agent.resolve_playbook(str(tmp_path)) == "docs/agent.md"


def test_default_playbook_ships_beside_wrapper():
    import os

    # The fallback path must actually resolve to a shipped file, or a repo
    # without its own playbook would point the agent at nothing.
    assert os.path.isfile(agent.DEFAULT_PLAYBOOK)
    assert agent.DEFAULT_PLAYBOOK.endswith("default-playbook.md")
