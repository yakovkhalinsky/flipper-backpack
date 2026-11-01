#!/usr/bin/env bash
# Install the flipper-runner Pi service. Run from this folder: sudo ./install.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
cd "$(dirname "$0")"
PREFIX=/opt/flipper-runner
USER_=flipper-runner

echo "== packages"
apt-get install -y python3-serial >/dev/null

echo "== user"
id "$USER_" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin "$USER_"
usermod -aG dialout,video "$USER_"   # serial port; video for vcgencmd

echo "== code + tools (root-owned, read-only to the service)"
install -d -o root -g root -m 755 "$PREFIX" "$PREFIX/tools" "$PREFIX/flipper_runner" "$PREFIX/schema"
install -o root -g root -m 644 flipper_runner/*.py "$PREFIX/flipper_runner/"
install -o root -g root -m 644 flipper_tool.py kismet_client.py kismet_tools.py \
  evilcrow_serial.py evilcrow_tools.py evilcrow_reader.py "$PREFIX/"
install -o root -g root -m 644 schema/output-v1.schema.json "$PREFIX/schema/"
for d in tools/*/; do
  id=$(basename "$d")
  install -d -o root -g root -m 755 "$PREFIX/tools/$id"
  install -o root -g root -m 644 "$d/manifest.json" "$PREFIX/tools/$id/"
  install -o root -g root -m 755 "$d/run" "$PREFIX/tools/$id/"
done

echo "== config dir"
install -d -o root -g "$USER_" -m 750 /etc/flipper-runner

echo "== sudoers (validated before install)"
tmp=$(mktemp)
install -m 440 sudoers.flipper-runner "$tmp"
visudo -cf "$tmp"
install -o root -g root -m 440 "$tmp" /etc/sudoers.d/flipper-runner
rm -f "$tmp"
visudo -c >/dev/null

echo "== UART: enable, serial console off"
if command -v raspi-config >/dev/null; then
  raspi-config nonint do_serial_cons 1   # login shell over serial: off
  raspi-config nonint do_serial_hw 0     # serial hardware: on
else
  CFG=/boot/firmware/config.txt; [[ -f $CFG ]] || CFG=/boot/config.txt
  grep -q '^enable_uart=1' "$CFG" || echo 'enable_uart=1' >> "$CFG"
  CMD=/boot/firmware/cmdline.txt; [[ -f $CMD ]] || CMD=/boot/cmdline.txt
  sed -i -E 's/console=(serial0|ttyAMA0|ttyS0),[0-9]+ ?//g' "$CMD"
fi
systemctl disable --now serial-getty@serial0.service serial-getty@ttyS0.service \
  serial-getty@ttyAMA0.service 2>/dev/null || true

echo "== service"
install -o root -g root -m 644 flipper-runner.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable flipper-runner.service
systemctl restart flipper-runner.service || true

echo
echo "Done. If the UART settings changed, reboot once: sudo reboot"
echo "Logs: journalctl -u flipper-runner -f"
