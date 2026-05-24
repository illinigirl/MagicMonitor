"""
Pytest fixtures for the LLM eval suite.

Responsibilities:
  - Load mcp/.env so ANTHROPIC_API_KEY is available without manual export.
  - Fail loudly if no API key is set — the eval suite can't run without one.
  - Provide a shared Anthropic client fixture so each case doesn't re-init.

Note: this conftest is scoped to mcp/evals/ — it does NOT affect the
regular mcp/tests/ suite (which has its own conftest.py).
"""

import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_MCP_ROOT = _HERE.parent
_ENV_FILE = _MCP_ROOT / ".env"

# Add both `mcp/evals/` (for sibling-module imports like `from runner
# import ...`) and `mcp/` (for `import server` if an assertion ever
# needs to introspect the canonical MCP tool registry).
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_MCP_ROOT))


def pytest_configure(config: pytest.Config) -> None:
    """Load mcp/.env before any test runs.

    python-dotenv is silent if the file doesn't exist; we check explicitly
    so the user gets a clear error rather than a cryptic "missing API key"
    failure deep in the runner.
    """
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE)

    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.exit(
            "ANTHROPIC_API_KEY is not set. Either:\n"
            f"  - Add ANTHROPIC_API_KEY=... to {_ENV_FILE}\n"
            "  - Or export it in your shell before running the evals.\n"
            "See mcp/evals/README.md for setup.",
            returncode=2,
        )


@pytest.fixture(scope="session")
def anthropic_client():
    """Single shared Anthropic client across all eval cases in a run.

    Session-scoped because the client is stateless and reusing it
    avoids the connection-setup overhead per case.
    """
    import anthropic

    return anthropic.Anthropic()
