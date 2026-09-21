"""Validate the GitHub Actions workflow files.

A broken workflow file gets one line of feedback from GitHub, "this run
likely failed because of a workflow file issue", with no indication of where.
Parsing them locally turns that into a line number. It also catches the
specific mistake that produced that message once here: a shell heredoc inside
a YAML block scalar, where the quoting rules of the two languages disagree.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs it with the dev extra
    print("pyyaml is not installed; skipping workflow validation")
    raise SystemExit(0) from None

REQUIRED_KEYS = {"jobs"}


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    workflows = sorted((root / ".github" / "workflows").glob("*.yml"))
    if not workflows:
        print("no workflow files found", file=sys.stderr)
        return 1

    failed = False
    for path in workflows:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            print(f"{path.name}: {exc}", file=sys.stderr)
            failed = True
            continue
        if not isinstance(document, dict) or not set(document) >= REQUIRED_KEYS:
            print(f"{path.name}: missing a top-level 'jobs' block", file=sys.stderr)
            failed = True
            continue
        for name, job in document["jobs"].items():
            if "steps" not in job and "uses" not in job:
                print(f"{path.name}: job '{name}' has neither steps nor uses", file=sys.stderr)
                failed = True
        print(f"{path.name}: ok ({len(document['jobs'])} jobs)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
