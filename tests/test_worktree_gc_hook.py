"""The session-start worktree collector, driven through its real interface.

The CLI is stubbed, never invoked for real: this hook's whole job is to delete
directories, and a test that reached the installed `agent` would collect the
developer's live worktrees as a side effect of running the suite.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "worktree-gc.sh"


@pytest.fixture
def home(tmp_path):
    """A HOME whose `~/.local/bin/agent` is a stub the test controls."""
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    return tmp_path


def _stub_agent(home, stdout="", exit_code=0):
    agent = home / ".local" / "bin" / "agent"
    agent.write_text(
        "#!/usr/bin/env bash\ncat <<'STUB_EOF'\n" + stdout + f"STUB_EOF\nexit {exit_code}\n"
    )
    agent.chmod(0o755)
    return agent


def _run(home, payload=None, env=None, args=()):
    return subprocess.run(
        ["bash", str(HOOK), *args],
        input=json.dumps(payload if payload is not None else {"source": "clear"}),
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(home), **(env or {})},
    )


def test_reports_what_it_collected_to_the_human(home):
    _stub_agent(home, "gc: removed session-a (merged)\ngc: removed session-b (idle)\n")
    result = _run(home)
    assert result.returncode == 0
    message = json.loads(result.stdout)["systemMessage"]
    assert "Collected 2 spent session worktree(s)" in message
    assert "session-a (merged)" in message
    assert "session-b (idle)" in message


def test_says_nothing_when_nothing_was_collected(home):
    """Silence is the common case; a line every session start is a line nobody reads."""
    _stub_agent(home, "")
    assert _run(home).stdout == ""


def test_a_kept_dirty_worktree_is_not_reported_as_collected(home):
    """`gc` prints what it kept too. Only removals are news."""
    _stub_agent(home, "gc: keeping session-c (dirty)\n")
    assert _run(home).stdout == ""


def test_the_opt_out_skips_the_run_entirely(home):
    marker = home / "ran"
    agent = _stub_agent(home, "gc: removed session-a (merged)\n")
    agent.write_text(
        f'#!/usr/bin/env bash\ntouch {marker}\necho "gc: removed session-a (merged)"\n'
    )
    agent.chmod(0o755)
    assert _run(home, env={"AGENT_WORKTREE_GC": "0"}).stdout == ""
    assert not marker.exists()


def test_subagents_do_not_each_collect(home):
    """Concurrent subagents would otherwise race over the same worktrees."""
    _stub_agent(home, "gc: removed session-a (merged)\n")
    assert _run(home, payload={"source": "startup", "agent_type": "Explore"}).stdout == ""


def test_ci_never_collects(home):
    """CI checkouts are not session worktrees, and the hooks are not loaded there anyway."""
    _stub_agent(home, "gc: removed session-a (merged)\n")
    assert _run(home, env={"CI": "1"}).stdout == ""


def test_a_missing_cli_is_silent_not_fatal(home, tmp_path):
    """A host mid-bootstrap has no installed agent yet. That is not a session's problem."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    result = _run(home, env={"PATH": f"{empty}:/usr/bin:/bin"})
    assert result.returncode == 0
    assert result.stdout == ""
