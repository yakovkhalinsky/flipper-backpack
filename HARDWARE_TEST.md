# Optional hardware test checklist

- [ ] Pi flashed with Raspberry Pi OS (Bookworm or later), booted on the backpack, SSH works.
- [ ] `sudo ./install.sh` finishes; `sudo visudo -c` OK; reboot once if UART settings changed.
- [ ] `ls -l /dev/serial0` points at `ttyS0` (Zero 2W mini-UART when Bluetooth is on) or `ttyAMA0`.
- [ ] `systemctl status flipper-runner` active; `journalctl -u flipper-runner` shows "7 tools".
- [ ] `systemctl status serial-getty@ttyS0` inactive (no login console fighting for the port).
- [ ] Flipper: Settings → Expansion Modules → **None** (see README), USB-UART bridge app closed.
- [ ] Flipper: Apps → GPIO → Pi Command Runner opens and lists 7 tools.
- [ ] System info shows host, IP, uptime and a real CPU temp (vcgencmd works via `video` group).
- [ ] Wi-Fi shows the SSID; Disk shows a table; Services → ssh shows a status popup.
- [ ] Back while a tool is running cancels it (journal: `reason=cancelled`).
- [ ] Stop the service (`sudo systemctl stop flipper-runner`), open the app → "Pi not responding";
      start it again, press Retry → menu appears.
- [ ] Cold boot test: power both at once, open the app straight away → "Pi not responding"
      (~25–40 s to boot), Retry later works.
- [ ] Reboot → confirm dialog → No returns to menu; Yes reboots the Pi.
- [ ] `sudo -u flipper-runner sudo -n /usr/bin/systemctl status` is **refused**.
- [ ] Shutdown → Yes powers the Pi off (wait for the green LED to stop before removing power).
