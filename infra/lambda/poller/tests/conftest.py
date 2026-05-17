"""
Shared test fixtures for the poller test suite.

Mirrors the Lambda runtime's sys.path: the function root is added so
test files can `import weather`, `import db`, etc. — the same imports
the Lambda's handler uses in production. This keeps tests honest to
how the code actually runs in AWS.
"""

import os
import sys
from pathlib import Path

# Put the function root on sys.path BEFORE any module imports happen.
# Matches the Lambda runtime layout.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

# Env vars required by db.py at import time. Stubbed for tests — the
# table name is never actually hit because tests inject stub tables.
os.environ.setdefault("DISNEY_TABLE_NAME", "stub-test-table")
os.environ.setdefault("PUSHOVER_APP_TOKEN_PARAM", "/stub/test/token")
os.environ.setdefault("PARK_KEYS", "magic_kingdom,epcot,hollywood_studios,animal_kingdom")
