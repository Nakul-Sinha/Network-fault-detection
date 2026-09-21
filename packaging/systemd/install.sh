#!/usr/bin/env bash
# Install NetPulse Local as a systemd user service.
set -euo pipefail

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_NAME="netpulse.service"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${UNIT_NAME}"

if ! command -v netpulse >/dev/null 2>&1; then
  echo "netpulse is not on PATH. Install it first:" >&2
  echo "  python3 -m pip install --user netpulse-local" >&2
  exit 1
fi

echo "Checking what this machine can measure..."
netpulse doctor || true

mkdir -p "$UNIT_DIR"
install -m 0644 "$SOURCE" "$UNIT_DIR/$UNIT_NAME"
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

cat <<MSG

NetPulse Local is running as a user service.

  status      systemctl --user status netpulse
  logs        journalctl --user -u netpulse -f
  health      netpulse status
  web UI      http://127.0.0.1:8787/
  stop        systemctl --user stop netpulse

To keep it running while you are logged out:
  loginctl enable-linger "$USER"
MSG
