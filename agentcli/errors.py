class AgentError(Exception):
    """Base for all agent CLI errors."""


class AgentAuthError(AgentError):
    """Credential fetch, mint, or token validation failure."""


class AgentHTTPError(AgentError):
    def __init__(self, message: str, status_code: int = 0, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AgentConfigError(AgentError):
    """Missing config, bad path, absent checkout."""


class AgentInputError(AgentError):
    """Bad CLI arguments."""


class AgentGitError(AgentError):
    """A git invocation failed, or a checkout is in a state we refuse to touch."""
