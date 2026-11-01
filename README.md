# Pi Command Runner for Flipper Zero + Raspberry Pi backpack

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
  tools/                  sysinfo, wifi, disk, services, service-status, reboot, shutdown
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
  `NoNewPrivileges` is intentionally off so sudo works for the two power actions.

### sudoers (installed as `/etc/sudoers.d/flipper-runner`, validated with `visudo -c`)
```
flipper-runner ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff
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

## Uninstall
`sudo ./uninstall.sh` on the Pi (leaves the UART enabled), and delete the .fap.

## Tests
`cd pi && python3 -m unittest -v tests.test_service` — 22 tests over a pty pair: handshake,
discovery, every output type, invalid/oversize output → error view, param bounds, timeout
process-group kill, cancel, unknown/bad ids, oversize/non-printable frames, confirm enforcement.
