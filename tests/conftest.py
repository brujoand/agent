"""Shared test setup.

issue_agent/ runs in production as a flat script directory (`python
/opt/issue-agent/agent.py` puts it on sys.path[0]); replicate exactly that so
`agent`, `providers`, and `s3_session_store` import as top-level modules here
too, instead of inventing a package shape production doesn't have.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "issue_agent"))
