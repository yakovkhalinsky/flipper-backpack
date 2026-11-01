# Optional hardware test checklist

- [ ] Pi flashed with Raspberry Pi OS (Bookworm or later), booted on the backpack, SSH works.
- [ ] `sudo ./install.sh` finishes; `sudo visudo -c` OK; reboot once if UART settings changed.
- [ ] `ls -l /dev/serial0` points at `ttyS0` (Zero 2W mini-UART when Bluetooth is on) or `ttyAMA0`.
- [ ] `systemctl status flipper-runner` active; `journalctl -u flipper-runner` shows "13 tools".
- [ ] `systemctl status serial-getty@ttyS0` inactive (no login console fighting for the port).
- [ ] Flipper: Settings → Expansion Modules → **None** (see README), USB-UART bridge app closed.
- [ ] Flipper: Apps → GPIO → Pi Command Runner opens and lists 13 tools.
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

## Kismet (optional)
Only on networks you own or are permitted to monitor.
- [ ] 64-bit Raspberry Pi OS bookworm (`dpkg --print-architecture` = `arm64`).
- [ ] USB monitor-mode adapter on the OTG port shows up in `iw dev` (e.g. `wlan1`); `source=` in
      `/etc/kismet/kismet_site.conf` matches it.
- [ ] `sudo ./install-kismet.sh` completes; `/etc/flipper-runner/kismet.env` is `-rw------- flipper-runner`.
- [ ] `sudo visudo -c` OK; `sudo -u flipper-runner sudo -n /usr/bin/systemctl restart kismet.service` is **refused**.
- [ ] Flipper: Kismet status with Kismet stopped → Service `inactive`, API "not reachable", log size/free disk shown.
- [ ] Kismet start → confirm → "Kismet started"; Kismet status shows the source `up chN`, packets/s and devices rising.
- [ ] `ss -ltnp | grep 2501` shows `127.0.0.1:2501` only (not `0.0.0.0`/`*`).
- [ ] Kismet devices (sort signal, limit 10) lists devices; picking one shows detail with a masked MAC;
      run Kismet device with `mac=full` to see it unmasked.
- [ ] Wait >5 min then pick a device from an old list → "Device list expired".
- [ ] Kismet alerts shows alerts or "No Kismet alerts".
- [ ] `free -m` / `systemctl status kismet` memory stays under the 340 MB cap after ~15 min.
- [ ] Kismet stop → confirm → "Kismet stopped".
