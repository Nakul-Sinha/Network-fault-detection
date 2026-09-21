"""Entry point for the frozen single-file binary.

``netpulse/__main__.py`` cannot be used here. PyInstaller bundles the entry
script as a top-level module with no parent package, so its
``from .cli import main`` fails at startup with "attempted relative import
with no known parent package". The binary builds cleanly and then dies on
first run, which is exactly the kind of failure a smoke test has to catch.

This module does the same job with an absolute import.
"""

from netpulse.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
