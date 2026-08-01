"""In-flight work: open PRs and session worktrees, before a session duplicates them.

The failure being prevented is concrete: #59 and #63 were the same change, written
three days apart by two agent sessions, because neither could see the other's open
PR. Every "return nothing" path here is deliberate -- this is advisory, and a
GitHub outage must cost a warning, not a session start.
"""

import subprocess

import pytest

from agentcli import inflight


def _pr(number=1, title="t", branch="b", files=None, total=None, base="main"):
    files = files if files is not None else []
    return inflight.PullRequest(
        number=number,
        title=title,
        branch=branch,
        files=files,
        total_files=total if total is not None else len(files),
        base=base,
    )


def test_line_names_the_branch_and_the_files():
    line = _pr(59, "raise the window", "chore/window", ["settings/settings.json"]).line()
    assert "#59" in line
    assert "[chore/window]" in line
    assert "settings/settings.json" in line


def test_line_reports_truncation_rather_than_hiding_it():
    line = _pr(1, "t", "b", ["a", "b", "c", "d"], total=9).line()
    assert "+5 more" in line


def test_line_without_files_has_no_dangling_clause():
    assert _pr(2, "t", "b", []).line() == "#2 [b] t"


def test_open_pull_requests_parses_gh_output(monkeypatch):
    payload = [
        {
            "number": 59,
            "title": "raise the window",
            "headRefName": "chore/window",
            "files": [{"path": "settings/settings.json"}, {"path": "hooks/context-budget.sh"}],
        }
    ]
    monkeypatch.setattr(inflight, "_gh_json", lambda args, repo: payload)

    prs = inflight.open_pull_requests("o/r")
    assert [p.number for p in prs] == [59]
    assert prs[0].files == ["settings/settings.json", "hooks/context-budget.sh"]
    assert prs[0].total_files == 2


def test_open_pull_requests_caps_the_files_it_lists(monkeypatch):
    files = [{"path": f"f{i}"} for i in range(20)]
    monkeypatch.setattr(
        inflight,
        "_gh_json",
        lambda args, repo: [{"number": 1, "title": "t", "headRefName": "b", "files": files}],
    )
    pr = inflight.open_pull_requests("o/r")[0]
    assert len(pr.files) == inflight.FILES_PER_PR
    assert pr.total_files == 20


@pytest.mark.parametrize("bad", [None, {}, "not a list", 7])
def test_open_pull_requests_is_empty_on_anything_unexpected(monkeypatch, bad):
    monkeypatch.setattr(inflight, "_gh_json", lambda args, repo: bad)
    assert inflight.open_pull_requests("o/r") == []


def test_gh_json_returns_none_when_gh_fails(monkeypatch):
    monkeypatch.setattr(inflight.github, "token", lambda: "t")
    monkeypatch.setattr(
        inflight.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "boom"),
    )
    assert inflight._gh_json(["pr", "list"], "o/r") is None


def test_gh_json_returns_none_without_credentials(monkeypatch):
    def no_creds():
        raise RuntimeError("no key")

    monkeypatch.setattr(inflight.github, "token", no_creds)
    assert inflight._gh_json(["pr", "list"], "o/r") is None


def test_gh_json_returns_none_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(inflight.github, "token", lambda: "t")
    monkeypatch.setattr(
        inflight.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "not json", ""),
    )
    assert inflight._gh_json(["pr", "list"], "o/r") is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/brujoand/agent.git", "brujoand/agent"),
        ("https://github.com/brujoand/agent", "brujoand/agent"),
        ("git@github.com:brujoand/agent.git", "brujoand/agent"),
    ],
)
def test_slug_for_parses_remote_urls(monkeypatch, url, expected):
    monkeypatch.setattr(
        inflight.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, url + "\n", ""),
    )
    assert inflight.slug_for("agent") == expected


def test_slug_for_is_none_without_a_remote(monkeypatch):
    monkeypatch.setattr(
        inflight.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    assert inflight.slug_for("agent") is None


def test_report_combines_pull_requests_and_worktrees(monkeypatch):
    monkeypatch.setattr(inflight, "slug_for", lambda repo: "o/r")
    monkeypatch.setattr(inflight, "open_pull_requests", lambda slug, limit=10: [_pr(59)])
    monkeypatch.setattr(inflight, "local_worktrees", lambda repo: ["feat/x"])

    lines = inflight.report("agent")
    assert "1 open pull request(s) on o/r:" in lines[0]
    assert "#59" in lines[1]
    assert "feat/x" in lines[-1]


def test_report_is_empty_when_the_repo_is_quiet(monkeypatch):
    monkeypatch.setattr(inflight, "slug_for", lambda repo: "o/r")
    monkeypatch.setattr(inflight, "open_pull_requests", lambda slug, limit=10: [])
    monkeypatch.setattr(inflight, "local_worktrees", lambda repo: [])
    assert inflight.report("agent") == []


def test_context_block_carries_the_instruction_not_just_the_facts(monkeypatch):
    """The failure was not "did not know" -- it was "knew and did not act on it",
    so the injected block has to say what to do about the overlap."""
    monkeypatch.setattr(inflight, "report", lambda repo: ["1 open pull request(s):", "  #59 x"])
    block = inflight.context_block("agent")
    assert "#59" in block
    assert "BEFORE writing" in block
    assert "instead of opening a second PR" in block


def test_context_block_is_empty_when_nothing_is_in_flight(monkeypatch):
    monkeypatch.setattr(inflight, "report", lambda repo: [])
    assert inflight.context_block("agent") == ""


def test_repo_for_delegates_to_the_freshness_resolver(monkeypatch, tmp_path):
    checkout = tmp_path / "demo"
    monkeypatch.setattr(inflight.freshness, "checkout_for", lambda path: checkout)
    assert inflight.repo_for(tmp_path) == "demo"


def test_repo_for_is_none_outside_a_managed_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(inflight.freshness, "checkout_for", lambda path: None)
    assert inflight.repo_for(tmp_path) is None


def test_open_pull_requests_records_the_base_branch(monkeypatch):
    """Without `baseRefName` a stack is invisible, which is how three PRs were lost."""
    monkeypatch.setattr(
        inflight,
        "_gh_json",
        lambda args, repo: [
            {"number": 7, "title": "t", "headRefName": "child", "baseRefName": "parent"}
        ],
    )
    assert inflight.open_pull_requests("o/r")[0].base == "parent"


def test_nothing_is_stacked_when_every_pr_targets_the_default():
    prs = [_pr(1, branch="a"), _pr(2, branch="b")]
    assert inflight.stacked(prs, "main") == []
    assert inflight.stack_lines(prs, "main") == []


def test_a_pr_based_on_another_open_pr_is_stacked():
    parent = _pr(63, branch="parent")
    child = _pr(64, branch="child", base="parent")
    assert inflight.stacked([parent, child], "main") == [(child, parent)]


def test_stack_line_states_the_order_that_keeps_the_child_alive():
    line = inflight.stack_lines(
        [_pr(63, branch="parent"), _pr(64, branch="child", base="parent")], "main"
    )[0]
    assert "#64" in line and "#63" in line
    assert "BEFORE" in line
    assert "switch its base to main" in line


def test_a_base_with_no_open_pr_is_still_reported():
    """The parent already merged or was closed -- the child is orphaned now, not later."""
    child = _pr(64, branch="child", base="gone")
    assert inflight.stacked([child], "main") == [(child, None)]
    assert "'gone'" in inflight.stack_lines([child], "main")[0]


def test_master_default_is_not_mistaken_for_a_stack():
    """Repos here use both `main` and `master`; the default is passed in, never assumed."""
    assert inflight.stacked([_pr(1, branch="a", base="master")], "master") == []
