#!/usr/bin/env python3
"""CI guard for the data-growth invariants (see DATA-GROWTH-MODEL.md).

Turns the silent-data-growth failure class from something we inspect for
into something the build won't let us ship. Fails (exit 1) when someone:

  1. Adds a `table.scan()` on an interactive path (mcp/, poller) without a
     `# bounded-scan: <reason>` justification. A Scan is O(table size), and
     the unbounded WAIT#/HIST#/FORECAST# types make that grow without
     bound. Use a keyed Query/GSI instead — or justify inline. The nightly
     aggregator (tools/) is the one sanctioned full-table scanner and is
     out of scope here by design (offline, paginated, no request timeout).

  2. Writes an unbounded SK (WAIT#/HIST#/FORECAST#) without a `ttl`. No TTL
     = infinite growth. A new unbounded type must be added to the model doc
     AND given a ttl.

Run: `python tools/check_growth_invariants.py` (exit 0 = clean).
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNBOUNDED_PREFIXES = ("WAIT#", "HIST#", "FORECAST#")

# Interactive code whose scans must be keyed. tools/ (aggregator, backfill)
# is deliberately excluded — those are offline, paginated, and must read the
# whole table.
_INTERACTIVE_PY = ["mcp/server.py", "mcp/server_http.py", "mcp/_tool_impls.py"]
_POLLER_GLOB = "infra/lambda/poller/*.py"
_WEB_GLOB = "web/src/**/*.ts"
# Files that legitimately write the unbounded SK rows (checked for ttl).
_UNBOUNDED_WRITERS = ["infra/lambda/poller/db.py"]


def _sk_prefix(node: ast.AST) -> str | None:
    """The literal SK prefix from a Constant ("STATE") or an f-string
    (f"WAIT#{ts}" → "WAIT#"). None if not a string-ish SK value."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def check_scans() -> list[str]:
    problems: list[str] = []
    py = [ROOT / p for p in _INTERACTIVE_PY] + list(ROOT.glob(_POLLER_GLOB))
    for f in py:
        if not f.exists() or "test" in f.name:
            continue
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if ".scan(" in line and "def scan" not in line and "# bounded-scan:" not in line:
                problems.append(
                    f"{f.relative_to(ROOT)}:{i}: table.scan() on an interactive "
                    "path — use a keyed Query/GSI, or justify with "
                    "`# bounded-scan: <reason>`. (DATA-GROWTH-MODEL.md)"
                )
    for f in ROOT.glob(_WEB_GLOB):
        if ".test." in f.name:
            continue
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if "ScanCommand" in line and "// bounded-scan:" not in line:
                problems.append(
                    f"{f.relative_to(ROOT)}:{i}: ScanCommand — use a keyed "
                    "Query/GSI, or justify with `// bounded-scan: <reason>`."
                )
    return problems


def check_ttls() -> list[str]:
    """Every dict literal that sets SK to an unbounded prefix must also set
    a 'ttl'. AST-based so it can't be fooled by formatting."""
    problems: list[str] = []
    for rel in _UNBOUNDED_WRITERS:
        f = ROOT / rel
        if not f.exists():
            continue
        tree = ast.parse(f.read_text(), str(f))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            sk_val = None
            has_ttl = False
            for k, v in zip(node.keys, node.values):
                key = k.value if isinstance(k, ast.Constant) else None
                if key == "SK":
                    sk_val = v
                elif key == "ttl":
                    has_ttl = True
            if sk_val is None:
                continue
            prefix = _sk_prefix(sk_val)
            if prefix and prefix.startswith(UNBOUNDED_PREFIXES) and not has_ttl:
                problems.append(
                    f"{f.relative_to(ROOT)}:{sk_val.lineno}: write to unbounded "
                    f"SK '{prefix}…' without a 'ttl' — infinite growth. Add a "
                    "ttl (DATA-GROWTH-MODEL.md)."
                )
    return problems


def main() -> int:
    problems = check_scans() + check_ttls()
    if problems:
        print("Data-growth invariant violations:\n", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        print(
            "\nSee DATA-GROWTH-MODEL.md. These rules keep the "
            "silent-data-growth failure class un-introducible.",
            file=sys.stderr,
        )
        return 1
    print("Data-growth invariants OK (no unjustified scans; unbounded writes TTL'd).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
