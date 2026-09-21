# PyInstaller spec for a single-file netpulse executable.
#
# This is the "try it without installing Python" path from PRD 14.3. It is a
# console binary rather than a windowed one on purpose: the whole CLI has to
# keep working, and `netpulse run` prints the web UI address it is serving.
#
# Build:
#   python -m pip install ".[package]"
#   pyinstaller packaging/pyinstaller/netpulse.spec --clean --noconfirm

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(os.getcwd())

# The trained model and the web UI are package data, not code, so they have
# to be named explicitly or the binary ships without a forecast head and
# without a UI. The CI wheel check guards the same thing for the wheel.
datas = [
    (str(ROOT / "netpulse" / "ml" / "weights" / "l3_default.json"), "netpulse/ml/weights"),
    (str(ROOT / "netpulse" / "api" / "static"), "netpulse/api/static"),
]
datas += collect_data_files("netpulse", includes=["py.typed"])

# The CLI imports its heavier subsystems inside the functions that need
# them, so the whole package is collected rather than relying on static
# analysis to find every one of them.
hiddenimports = collect_submodules("netpulse")
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

analysis = Analysis(
    # Not netpulse/__main__.py: PyInstaller bundles the entry script as a
    # top-level module with no parent package, so its relative import fails
    # at startup. entry.py does the same job with an absolute import.
    [str(ROOT / "packaging" / "pyinstaller" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here needs a plotting stack or a notebook kernel; excluding
    # them keeps the binary to a size someone will actually download.
    excludes=["matplotlib", "tkinter", "pandas", "scipy", "IPython", "pytest"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="netpulse",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
