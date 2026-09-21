"""Check that a generated SBOM is a real one.

PRD 14.3 lists a software bill of materials as a shipping requirement. The
first version of the release step passed a flag cyclonedx-py does not accept
and hid the failure behind `|| echo skipped`, so v0.1.0 went out without an
SBOM and nothing in the log said so.

This makes the check explicit: the file has to parse, declare CycloneDX, and
actually list components. Running it is what turns "we generate an SBOM" from
a claim into a fact.

    python scripts/check_sbom.py dist/sbom.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MIN_COMPONENTS = 5


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    path = Path(argv[1])
    if not path.exists():
        print(f"no SBOM at {path}", file=sys.stderr)
        return 1

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"{path} is not valid JSON: {exc}", file=sys.stderr)
        return 1

    fmt = document.get("bomFormat")
    if fmt != "CycloneDX":
        print(f"{path} declares bomFormat {fmt!r}, expected CycloneDX", file=sys.stderr)
        return 1

    components = document.get("components") or []
    if len(components) < MIN_COMPONENTS:
        print(
            f"{path} lists {len(components)} components, which is too few to be real",
            file=sys.stderr,
        )
        return 1

    print(
        f"SBOM ok: {fmt} {document.get('specVersion')}, {len(components)} components, "
        f"{path.stat().st_size} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
