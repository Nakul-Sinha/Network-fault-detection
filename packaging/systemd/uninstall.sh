#!/usr/bin/env bash
# Remove the NetPulse Local user service. Stored data is left alone unless
# --purge is given, because deleting someone's history by surprise is rude.
set -euo pipefail

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
systemctl --user disable --now netpulse.service 2>/dev/null || true
rm -f "$UNIT_DIR/netpulse.service"
systemctl --user daemon-reload

if [[ "${1:-}" == "--purge" ]]; then
  netpulse wipe --yes 2>/dev/null || true
  rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/netpulse" \
         "${XDG_STATE_HOME:-$HOME/.local/state}/netpulse" \
         "${XDG_CONFIG_HOME:-$HOME/.config}/netpulse"
  echo "Service removed and all stored data deleted."
else
  echo "Service removed. Stored data kept; run with --purge to delete it too."
fi
