#!/usr/bin/env bash
# Remove flipper-runner. Leaves the UART enabled (re-enable the console with raspi-config if wanted).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
systemctl disable --now flipper-runner.service 2>/dev/null || true
rm -f /etc/systemd/system/flipper-runner.service
systemctl daemon-reload
rm -f /etc/sudoers.d/flipper-runner
rm -rf /opt/flipper-runner /etc/flipper-runner   # includes kismet.env (API key)
userdel flipper-runner 2>/dev/null || true
echo "Removed. UART left enabled; run 'sudo raspi-config' to change serial settings."
