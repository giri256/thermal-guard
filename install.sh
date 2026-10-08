#!/bin/sh
# Install thermal-guard as a systemd service:   sudo sh install.sh
# Remove it (restores the original clocks):      sudo sh install.sh --uninstall
set -e
cd "$(dirname "$0")"
LIB=/usr/local/lib/thermal-guard
UNIT=/etc/systemd/system/thermal-guard.service

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

if [ "${1:-}" = --uninstall ]; then
    systemctl disable --now thermal-guard 2>/dev/null || true
    rm -rf "$LIB" "$UNIT"
    systemctl daemon-reload
    echo "thermal-guard removed"
    exit 0
fi

ls /sys/class/drm/card*/gt_max_freq_mhz >/dev/null 2>&1 || echo "warning: no i915 GPU found; only --turbo-control will have an effect"
install -Dm755 thermal-guard.py "$LIB/thermal-guard.py"
install -Dm644 thermal-guard.service "$UNIT"
systemctl daemon-reload
systemctl enable --now thermal-guard
sleep 1
journalctl -u thermal-guard -n 1 --no-pager -o cat
echo "installed. Watch it work: journalctl -fu thermal-guard"
