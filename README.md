# Pi Command Runner for Flipper Zero + Raspberry Pi backpack

[![CI](https://github.com/yakovkhalinsky/flipper-backpack/actions/workflows/ci.yml/badge.svg)](https://github.com/yakovkhalinsky/flipper-backpack/actions/workflows/ci.yml) [![Release](https://github.com/yakovkhalinsky/flipper-backpack/actions/workflows/release.yml/badge.svg)](https://github.com/yakovkhalinsky/flipper-backpack/actions/workflows/release.yml)

Run a registry of allowlisted tools on the Pi Zero 2W in the Elecrow/Binary-6 backpack and
see the results on the Flipper as menus, key/value lists, tables, status popups and progress
bars. Wired over the Flipper's USART: **pin 13 = TX, pin 14 = RX** ⇄ Pi `/dev/serial0`,
115200 8N1. (The backpack's PCF8523 RTC on I²C isn't used by this app.)

```
flipper/command_runner/   Flipper app (.fap, C, ufbt)
pi/                       Pi service, tools, installer, tests
  flipper_runner/         service (protocol, registry, schema)
  flipper_tool.py         helper for tool authors
  schema/output-v1.schema.json
  tools/                  sysinfo, wifi, disk, services, service-status, reboot, shutdown,
                          kismet-{start,stop,status,devices,device,alerts},
                          evilcrow-{status,captures,capture,live}
  kismet_client.py        read-only Kismet REST client (localhost only)
  kismet/                 sample kismet_site.conf + kismet.service
  install-kismet.sh       optional Kismet installer
  evilcrow_serial.py      read-only USB-serial parser + empty write allowlist
  evilcrow_reader.py      background ring-buffer reader (systemd unit)
  evilcrow/               udev rule + evilcrow-reader.service
  install-evilcrow.sh     optional Evil Crow RF V2 installer
  examples/git-pull/      disabled example
PROTOCOL.md  WRITING_TOOLS.md  HARDWARE_TEST.md
dist/pi_command_runner.fap   prebuilt (SDK release 1.5.1, API 88.2)
```

## Quick start
1. **Flipper:** copy `dist/pi_command_runner.fap` to `SD:/apps/GPIO/` (qFlipper → File manager,
   or put the SD card in a reader). It appears under Apps → GPIO → Pi Command Runner.
   Rebuild with `pip install ufbt && cd flipper/command_runner && ufbt` (or `ufbt launch` over USB).
2. **Pi:** `scp -r pi/ pi@<pi>:~/flipper-runner && ssh pi@<pi> 'cd ~/flipper-runner && sudo ./install.sh'`,
   then `sudo reboot` once so the UART changes apply.
3. **Flipper:** Settings → Expansion Modules → set the listen UART to **None** (see below), open the app.

`journalctl -u flipper-runner -f` shows one line per run: `run id=disk exit=0 reason=ok dur=0.1s`
(timestamp from the journal; tool output is never logged).

## How it works
* The Flipper sends `HELLO 1`, then `LIST`, and builds its menu from the tool manifests.
* Picking a tool asks for its params (enum → submenu, int → number input), shows a Yes/No
  dialog for `confirm` tools, then sends `RUN <id> name=value`.
* While running: a "running…" screen (or progress bar if the tool streams progress).
  **Back cancels** (the Pi kills the whole process group). Results show with the exit code;
  Back returns to the menu. `list` results chain into another tool.
* No answer to the handshake (Pi missing or still booting) → **"Pi not responding"** with Retry/Exit.

Details: [PROTOCOL.md](PROTOCOL.md). Adding tools: [WRITING_TOOLS.md](WRITING_TOOLS.md).

## Security model
* **The registry is the allowlist.** Only tools installed under `/opt/flipper-runner/tools/`,
  root-owned and not group/world-writable, are loadable. The UART carries ids and typed
  param values only — never command strings. Params are enum or bounded int (no free text in v1).
* Tools are exec'd with an argv list (`shell=False`), as the dedicated system user
  `flipper-runner` (no login shell, no home), stdin closed, minimal env, own session.
* Per-tool timeout (≤300 s) then SIGTERM → SIGKILL to the **process group**; stdout capped at 16 KB.
* Output must validate against schema v1 with hard caps; it's re-encoded as sanitised
  printable ASCII. Anything invalid becomes an error view, never raw output.
* Frames over 256 bytes or with non-printables are rejected; ids/values are regex-checked.
* `confirm` tools need `@confirm` on the wire as well as the dialog on the Flipper.
* systemd hardening: `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, etc.
  `NoNewPrivileges` is intentionally off so sudo works for the power and Kismet start/stop actions.

### sudoers (installed as `/etc/sudoers.d/flipper-runner`, validated with `visudo -c`)
```
flipper-runner ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff
flipper-runner ALL=(root) NOPASSWD: /usr/bin/systemctl start kismet.service, /usr/bin/systemctl stop kismet.service
```
Exact commands with exact arguments; nothing else is allowed. Remove the line to disable
remote power control.

Anyone who can talk to the UART (i.e. holds the Flipper while the app runs) can run any
installed tool — keep the registry to things you're happy for the Flipper to trigger.

## UART conflicts and caveats
* **Expansion Modules:** firmware ≥0.98 listens for expansion modules on pins 13/14. The app
  disables that service while it runs and re-enables it on exit, but if anything else holds the
  port, set **Settings → Expansion Modules → None**.
* **USB-UART bridge** (GPIO app) and other GPIO-UART apps use the same USART — close them first.
  The app acquires the port with `furi_hal_serial_control_acquire()` and releases it on exit;
  if it's busy the app exits immediately (check logs: "USART busy").
* **Pi side:** the serial login console must be off (install.sh does this with
  `raspi-config nonint do_serial_cons 1` + `do_serial_hw 0`, or `enable_uart=1` and the
  `console=serial0` entry removed). On the Zero 2W `/dev/serial0` is the mini-UART while
  Bluetooth is enabled; it's fine at 115200. Add `dtoverlay=disable-bt` if you see garbled frames.
* **First boot timing:** a cold Pi Zero 2W takes ~25–40 s to boot and start the service; the
  app tries `HELLO` 3× over ~4.5 s then shows "Pi not responding" — press Retry. Boot-time
  console noise is dropped by the frame filter.
* The "loading" spinner swallows all keys in the SDK, so during the brief handshake/list
  and cancel phases Back is ignored (each phase times out in ≤4.5 s). Tool runs use a
  custom "running…" screen instead so Back can cancel.
* Power: the backpack powers the Pi from the Flipper/USB; heavy tools can brown out on battery.

## Kismet (optional, passive monitoring)

> **Legal:** only monitor networks and devices you own or have explicit permission to monitor.
> These tools are **passive only**: no deauth, injection or other active features, and no
> Kismet active/attack plugins are installed or enabled.

| Tool | Output | What it does |
|---|---|---|
| Kismet start / stop | status (confirm) | `sudo systemctl start/stop kismet.service` (exact sudoers lines) |
| Kismet status | kv | service state, uptime, data sources + state, packets/s, device count, log size, free disk |
| Kismet devices | list | top N (`limit` 1–20) devices seen in the last hour, `sort` = `last_seen` or `signal`; each chains to Kismet device |
| Kismet device | kv | type, name/SSID, MAC (`mac` = `masked` (default, OUI only) or `full`), channel, signal, manufacturer, first/last seen, packets |
| Kismet alerts | text | most recent Kismet alerts |

**Hardware:** the Zero 2W's onboard Wi-Fi (brcmfmac) **does not support monitor mode** on stock
firmware. Use a USB Wi-Fi adapter that does (e.g. MT7601U/MT7612U/RT5370/AR9271-based) on the
micro-USB **OTG** port (an OTG adapter or powered hub). Nexmon can patch the onboard chip but is
unsupported here. Kismet is heavy for 512 MB RAM: the shipped `pi/kismet/kismet_site.conf`
keeps logging to the `kismet` DB only with short packet/device timeouts, ages devices out after
15 min, caps tracked devices and disables RRDs/history; the unit caps memory at 340 MB.

**Install:** after `install.sh`, run `sudo ./install-kismet.sh` (64-bit Raspberry Pi OS bookworm).
It adds the official Kismet apt repo with its key dearmored into
`/usr/share/keyrings/kismet-archive-keyring.gpg` (`signed-by=`), installs only `kismet-core` and
`kismet-capture-linux-wifi`, creates a `kismetd` user, installs the site conf and
`kismet.service` (not enabled at boot), and creates a **read-only role** API key in
`/etc/flipper-runner/kismet.env` (mode 0600, owned by `flipper-runner`; never printed or committed).
Then set your adapter in `source=` in `/etc/kismet/kismet_site.conf`.

**Security:** Kismet's httpd is bound to `127.0.0.1:2501` (`httpd_bind_address`) so the web UI
isn't on Wi-Fi; use `ssh -L 2501:127.0.0.1:2501 pi@<pi>` to reach it. The tools only talk to
`127.0.0.1:2501`, cap responses at 512 KB, and fall back to a status error view if Kismet is
down, unreachable or rejects the key.

**Device selection (why an index, not a key param):** the Flipper never sends a Kismet device key.
`Kismet devices` writes the keys it listed to `/run/flipper-runner/kismet-devices.json`
(0600, tmpfs, expires after 5 min) and each item chains with a bounded int `idx` (0–19).
`Kismet device` looks the key up there and re-validates it against `^[A-F0-9_:]{1,40}$`
before building the URL. This keeps schema v1 unchanged (still enum/int only) so no free-form
string ever crosses the UART or reaches a URL from the Flipper side.


## Evil Crow RF V2 (optional, receive / monitor only)

> **Legal:** receive and monitor only. Follow the radio rules that apply where you are.
> These tools **never transmit, replay, jam, brute-force or send rolling codes**. The Pi
> write-allowlist for the Crow's USB serial port is empty, and CI asserts that no TX
> command can be built or sent.

| Tool | Output | What it does |
|---|---|---|
| Evil Crow status | kv | `/dev/evilcrow` present?, baud, capture count, reader unit state |
| Evil Crow captures | list | recent RX captures from the ring buffer; each chains to Evil Crow capture |
| Evil Crow capture | kv | samples, symbol timing, short bit/sample preview (no raw dumps), age |
| Evil Crow live | progress → text | latest summary from the ring buffer |

**What the stock firmware actually supports over USB serial** (sources under
[joelsernamoreno/EvilCrowRF-V2](https://github.com/joelsernamoreno/EvilCrowRF-V2)):

* `firmware/firmware/firmware.ino` (current tip) calls `Serial.begin(38400)` but has **no**
  `Serial.available` / `Serial.read` command loop. Every RX `Serial.print` in
  `printReceived()` / `signalanalyse()` is **commented out**; captures go to the SD card
  (`/logs.txt`) and the Wi-Fi web UI (`/setrx`, `/stoprx`, `/logs`, …) only.
* `old-firmware/v1.3.3/EvilCrow-RFv2/EvilCrow-RFv2.ino` still printed `Count=…`, timing
  samples, `Samples/Symbol:`, bit strings and `Rawdata corrected:` to USB serial. Our
  parser understands that format when it appears.
* `old-firmware/other/ECRFv2-SDR/` is an **alternate** firmware with a HackRF-style serial
  command set (`rx_start`, `set_freq`, …). We do **not** speak that protocol, and we still
  refuse every TX/jam/replay token even if that firmware is flashed.

**Practical options for RX on the Flipper:**

1. Use firmware that still logs RX to serial (e.g. v1.3.x), or a small **receive-only**
   custom firmware that prints the same `Count=` blocks (not shipped here).
2. Start RX from the Crow's own web UI over Wi-Fi; the Pi reader will pick up serial logs
   if/when the firmware emits them.
3. Do **not** expect `evilcrow-rx-start` / frequency knobs over USB serial on stock tip —
   those controls are HTTP-only today, so they are not implemented here.

**Install:** after `install.sh`, plug the Crow into the Pi USB OTG port and run
`sudo ./install-evilcrow.sh`. It installs a udev rule (`/dev/evilcrow` for common ESP32
USB-UART VID:PIDs: CP210x `10c4:ea60`, CH340 `1a86:7523`, CH9102 `1a86:55d4`), the
`evilcrow-reader.service` (user `flipper-runner`, group `dialout`, no sudo), and refreshes
the tools. Override the port with `EVILCROW_PORT=` in `/etc/flipper-runner/evilcrow.env`
if your bridge isn't matched. The reader writes a bounded ring to
`/run/flipper-runner/evilcrow-captures.json` (tmpfs, 0600). Selection uses the same
index-into-cache pattern as the Kismet tools (`idx` 0–19, 5 min TTL).

## Uninstall
`sudo ./uninstall.sh` on the Pi (leaves the UART enabled), and delete the .fap.

## Tests
`cd pi && pip install -r requirements-dev.txt && python -m pytest -v tests` — service, Kismet
and Evil Crow tests (pty + mocked REST + serial allowlist).
