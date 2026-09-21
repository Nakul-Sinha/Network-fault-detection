"""Print the project's declared runtime dependencies, one per line.

Used by the CI dependency audit. Auditing the installed environment would
include this project itself, which has no PyPI entry to resolve, and
pip-audit under --strict treats an unresolvable distribution as a failure.
What the audit is for is the dependency tree that reaches users, so it reads
that straight from the project metadata.

    python scripts/runtime_requirements.py > requirements-runtime.txt
    pip-audit --strict --requirement requirements-runtime.txt
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    with open(root / "pyproject.toml", "rb") as handle:
        data = tomllib.load(handle)
    dependencies = data.get("project", {}).get("dependencies", [])
    if not dependencies:
        print("no runtime dependencies declared in pyproject.toml", file=sys.stderr)
        return 1
    for requirement in dependencies:
        print(requirement)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
