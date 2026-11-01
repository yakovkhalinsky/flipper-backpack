#!/usr/bin/env bash
# OPTIONAL: Evil Crow RF V2 receive-only USB serial support. Run after install.sh:
#   sudo ./install-evilcrow.sh
# - udev rule → /dev/evilcrow for common ESP32 USB-UART VID:PIDs
# - evilcrow-reader.service (flipper-runner user, dialout) keeps a ring buffer
# - no sudoers changes; the reader never writes to the Crow
# RECEIVE / MONITOR ONLY. Local radio rules still apply.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
cd "$(dirname "$0")"
RUNNER=flipper-runner
PREFIX=/opt/flipper-runner
ENVF=/etc/flipper-runner/evilcrow.env

id "$RUNNER" >/dev/null 2>&1 || { echo "run ./install.sh first"; exit 1; }

echo "== packages"
apt-get install -y python3-serial >/dev/null

echo "== code (evilcrow modules + tools already installed by install.sh if present)"
install -o root -g root -m 644 evilcrow_serial.py evilcrow_tools.py evilcrow_reader.py "$PREFIX/"
for d in tools/evilcrow-*/; do
  [[ -d $d ]] || continue
  id=$(basename "$d")
  install -d -o root -g root -m 755 "$PREFIX/tools/$id"
  install -o root -g root -m 644 "$d/manifest.json" "$PREFIX/tools/$id/"
  install -o root -g root -m 755 "$d/run" "$PREFIX/tools/$id/"
done

echo "== udev"
install -o root -g root -m 644 evilcrow/99-evilcrow.rules /etc/udev/rules.d/99-evilcrow.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty || true

echo "== env"
install -d -o root -g "$RUNNER" -m 750 /etc/flipper-runner
if [[ ! -f $ENVF ]]; then
  cat > "$ENVF" <<EOF
# Optional override. Default path is /dev/evilcrow (udev symlink).
# EVILCROW_PORT=/dev/ttyUSB0
EOF
  chown root:"$RUNNER" "$ENVF"
  chmod 640 "$ENVF"
fi

echo "== reader unit"
install -o root -g root -m 644 evilcrow/evilcrow-reader.service /etc/systemd/system/
# Ensure the runner user stays in dialout (install.sh also does this).
usermod -aG dialout "$RUNNER"
systemctl daemon-reload
systemctl enable evilcrow-reader.service
systemctl restart evilcrow-reader.service || true
systemctl try-reload-or-restart flipper-runner.service || true

echo
echo "Done. Plug the Evil Crow RF V2 into the Pi USB OTG port."
echo "  ls -l /dev/evilcrow"
echo "  journalctl -u evilcrow-reader -f"
echo "Stock tip firmware is silent on USB serial for RX — see README."
echo "Receiver tools: evilcrow-status / captures / capture / live."
