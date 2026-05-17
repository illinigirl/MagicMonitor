"""
Shared test fixtures for the MCP server test suite.

Adds mcp/ to sys.path so test files can `import server` directly — the
same import that Claude Desktop's subprocess loader does at startup.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
