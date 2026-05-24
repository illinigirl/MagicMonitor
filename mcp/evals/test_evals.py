"""
Pytest entry point for the eval suite.

Discovers every YAML case in mcp/evals/cases/, parameterizes a single
test function over them, runs each case end-to-end via the runner,
then evaluates the case's assertions.

A case file with no `assertions:` key produces a parametrized test
that will exercise the runner but not assert anything (useful for
smoke-testing a case before adding real assertions).

Eval runs cost real Anthropic API tokens. Don't run this in CI on
every PR. Recommended invocation:

    cd mcp
    pytest evals/ -v

Or to run just one case:

    pytest evals/ -v -k basic_mk_plan
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from runner import run_case
from assertions import run_assertions

_CASES_DIR = Path(__file__).resolve().parent / "cases"


def _discover_cases() -> list[tuple[str, dict]]:
    """Find every .yaml file under cases/ and load it.

    Returns list of (case_name, case_dict). Sorted by filename for
    deterministic test ordering.
    """
    cases: list[tuple[str, dict]] = []
    for path in sorted(_CASES_DIR.glob("*.yaml")):
        with path.open() as f:
            case = yaml.safe_load(f)
        if not isinstance(case, dict) or "name" not in case:
            raise ValueError(
                f"Case file {path} is malformed — must be a YAML "
                "object with at least a 'name' key."
            )
        cases.append((case["name"], case))
    return cases


_CASES = _discover_cases()


@pytest.mark.parametrize(
    "case",
    [c for _, c in _CASES],
    ids=[name for name, _ in _CASES],
)
def test_eval_case(anthropic_client, case: dict) -> None:
    """Run one eval case and check its assertions."""
    trace = run_case(anthropic_client, case)
    run_assertions(case, trace)
