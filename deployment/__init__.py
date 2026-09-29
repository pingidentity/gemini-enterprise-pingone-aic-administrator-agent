"""Customer deployment tooling for the Agent Runtime A2A service."""

import sys

MINIMUM_PYTHON = (3, 11)


def require_supported_python(version_info=sys.version_info) -> None:
    """Refuse interpreters the packaged runtime cannot run on.

    Agent Runtime installs the agent under the deploying interpreter's Python
    version, and the agent uses 3.11 features such as ``asyncio.timeout``.
    """
    if tuple(version_info[:2]) < MINIMUM_PYTHON:
        raise SystemExit(
            "Deploy with Python 3.11 or newer: Agent Runtime runs the agent on the "
            f"deploying interpreter's version, and {version_info[0]}."
            f"{version_info[1]} cannot run it."
        )


require_supported_python()
