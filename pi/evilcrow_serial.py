"""Read-only Evil Crow RF V2 USB-serial helpers.

Stock firmware (joelsernamoreno/EvilCrowRF-V2 `firmware/firmware/firmware.ino`)
opens Serial at 38400 but does NOT expose a command interface: there is no
Serial.available/read loop, and as of the 2025-10-18 tip every RX Serial.print
is commented out — captures go to the SD card and the Wi-Fi web UI only.

Older releases (e.g. old-firmware/v1.3.3) still printed Count=/sample lines and
"Rawdata corrected:" blocks to USB serial. This module parses that format when
it appears, and never writes to the port unless the command is on the empty
allowlist (i.e. we never write anything for stock firmware).

Sources (relative to https://github.com/joelsernamoreno/EvilCrowRF-V2):
  firmware/firmware/firmware.ino             Serial.begin(38400); RX Serial.print commented
  old-firmware/v1.3.3/.../EvilCrow-RFv2.ino  printReceived()/signalanalyse() Serial output
  old-firmware/other/ECRFv2-SDR/             alternate serial-cmd firmware (not used here)
"""
from __future__ import annotations

import glob
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Optional

BAUD = 38400

# Commands the Pi may WRITE. Empty on purpose: stock has no command interface,
# and we refuse every TX/replay/jam path even if an alternate firmware is flashed.
ALLOWED_WRITES: frozenset = frozenset()

FORBIDDEN_WRITE_PATTERNS = (
    "settx", "set_tx", "setjammer", "set_jammer", "stopjammer",
    "txconfig", "txprotocol", "txbinary", "txtesla",
    "jammer", "jam_", "replay", "brute", "bruteforce",
    "rolling", "rawtx", "raw_tx", "transmit", "tx_start",
    "tx_stop", "send_raw", "pushbutton",
)

KNOWN_VID_PID = (
    ("10c4", "ea60"),  # Silicon Labs CP210x
    ("1a86", "7523"),  # WCH CH340
    ("1a86", "55d4"),  # WCH CH9102
    ("303a", "1001"),  # Espressif USB JTAG/serial
)

DEFAULT_PORT_CANDIDATES = (
    "/dev/evilcrow",
    "/dev/serial/by-id/*EvilCrow*",
    "/dev/serial/by-id/*CP210*",
    "/dev/serial/by-id/*CH340*",
    "/dev/serial/by-id/*USB*",
)

COUNT_RE = re.compile(r"^Count=(\d+)\s*$")
SAMPLES_RE = re.compile(r"^Samples/Symbol:\s*(\d+)\s*$")
PAUSE_RE = re.compile(r"^\[Pause:\s*(\d+)\s*samples\]\s*$")
SEP_RE = re.compile(r"^-{10,}\s*$")
INT_CSV_RE = re.compile(r"^[\d,\s]+$")
BIN_RE = re.compile(r"^[01]{8,}$")

CAPTURES_FILE = "/run/flipper-runner/evilcrow-captures.json"
CAPTURES_MAX = 40
PREVIEW_SAMPLES = 8
PREVIEW_BITS = 32


class CrowError(Exception):
    def __init__(self, kind, message):
        self.kind = kind
        super().__init__(message)


@dataclass
class Capture:
    t: int
    count: int
    samples_us: List[int] = field(default_factory=list)
    symbol_us: Optional[int] = None
    binary: str = ""
    corrected_count: Optional[int] = None
    source: str = "serial"

    def preview(self) -> str:
        if self.binary:
            b = self.binary[:PREVIEW_BITS]
            return b + ("~" if len(self.binary) > PREVIEW_BITS else "")
        if self.samples_us:
            head = ",".join(str(x) for x in self.samples_us[:PREVIEW_SAMPLES])
            return head + ("~" if len(self.samples_us) > PREVIEW_SAMPLES else "")
        return "(empty)"

    def summary(self) -> str:
        bits = f" {len(self.binary)}b" if self.binary else ""
        sym = f" sym={self.symbol_us}us" if self.symbol_us else ""
        return f"n={self.count}{sym}{bits}"

    def to_dict(self):
        d = asdict(self)
        d["samples_us"] = self.samples_us[:64]
        d["binary"] = self.binary[:256]
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(
            t=int(d["t"]), count=int(d["count"]),
            samples_us=[int(x) for x in (d.get("samples_us") or [])],
            symbol_us=(int(d["symbol_us"]) if d.get("symbol_us") is not None else None),
            binary=str(d.get("binary") or ""),
            corrected_count=(int(d["corrected_count"]) if d.get("corrected_count") is not None else None),
            source=str(d.get("source") or "serial"),
        )


def assert_write_allowed(cmd: str) -> str:
    if not isinstance(cmd, str):
        raise CrowError("tx", "command must be a string")
    raw = cmd.strip()
    if not raw:
        raise CrowError("tx", "empty command")
    if "\n" in raw or "\r" in raw or "\x00" in raw:
        raise CrowError("tx", "multiline/null command refused")
    low = raw.lower()
    for pat in FORBIDDEN_WRITE_PATTERNS:
        if pat in low:
            raise CrowError("tx", f"refused forbidden pattern: {pat}")
    token = low.split()[0]
    if raw not in ALLOWED_WRITES and token not in ALLOWED_WRITES and low not in ALLOWED_WRITES:
        raise CrowError("tx", "command not on allowlist (receive-only)")
    return raw


def build_write(cmd: str) -> bytes:
    """Build bytes that would be written. Raises for anything not allowlisted."""
    allowed = assert_write_allowed(cmd)
    return (allowed + "\n").encode("ascii", errors="strict")


def find_port(env_port: Optional[str] = None,
              candidates: Iterable[str] = DEFAULT_PORT_CANDIDATES) -> Optional[str]:
    if env_port:
        return env_port if os.path.exists(env_port) else None
    for pat in candidates:
        if "*" in pat:
            for h in sorted(glob.glob(pat)):
                if os.path.exists(h):
                    return h
        elif os.path.exists(pat):
            return pat
    for pat in ("/dev/ttyUSB*", "/dev/ttyACM*"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


def port_info(path: Optional[str]) -> dict:
    if not path:
        return {"path": None, "exists": False, "real": None, "by_id": None}
    real = os.path.realpath(path) if os.path.exists(path) else None
    by_id = None
    if real:
        for p in sorted(glob.glob("/dev/serial/by-id/*")):
            try:
                if os.path.realpath(p) == real:
                    by_id = os.path.basename(p)
                    break
            except OSError:
                pass
    return {"path": path, "exists": bool(real), "real": real, "by_id": by_id}


class CaptureParser:
    """Stateful parser for v1.3.x Evil Crow Serial RX dumps."""

    def __init__(self):
        self._reset_block()
        self.captures: List[Capture] = []

    def _reset_block(self):
        self._count = None
        self._samples: List[int] = []
        self._symbol = None
        self._binary_parts: List[str] = []
        self._corrected = None
        self._in_corrected = False
        self._have_block = False

    def feed_line(self, line: str, now: Optional[int] = None) -> Optional[Capture]:
        line = line.strip()
        # Blank lines appear mid-block in v1.3.x dumps; only the dashed
        # separator (or a new top-level Count=) finalises a capture.
        if not line:
            return None
        if SEP_RE.match(line):
            return self._maybe_finish(now)

        m = COUNT_RE.match(line)
        if m:
            # Inside a "Rawdata corrected:" section, Count= is metadata for the
            # same capture — do not start a new block.
            if self._in_corrected and self._have_block:
                self._corrected = int(m.group(1))
                return None
            finished = self._maybe_finish(now) if self._have_block else None
            self._count = int(m.group(1))
            self._samples = []
            self._symbol = None
            self._binary_parts = []
            self._corrected = None
            self._in_corrected = False
            self._have_block = True
            return finished

        if line == "Rawdata corrected:":
            self._in_corrected = True
            return None
        if line == "Parsed Data:":
            return None

        m = SAMPLES_RE.match(line)
        if m:
            self._symbol = int(m.group(1))
            self._have_block = True
            return None

        m = PAUSE_RE.match(line)
        if m:
            self._have_block = True
            return None

        if BIN_RE.match(line):
            self._binary_parts.append(line)
            self._have_block = True
            return None

        if INT_CSV_RE.match(line) and "," in line:
            vals = [int(x) for x in line.split(",") if x.strip().isdigit()]
            if self._in_corrected:
                # Prefer Count= from the corrected header when present.
                if self._corrected is None:
                    self._corrected = len(vals)
            else:
                self._samples.extend(vals)
            self._have_block = True
            return None
        return None

    def _maybe_finish(self, now=None) -> Optional[Capture]:
        if not self._have_block or self._count is None:
            self._reset_block()
            return None
        cap = Capture(
            t=int(now if now is not None else time.time()),
            count=self._count,
            samples_us=self._samples[:512],
            symbol_us=self._symbol,
            binary="".join(self._binary_parts)[:512],
            corrected_count=self._corrected,
        )
        self.captures.append(cap)
        self._reset_block()
        return cap

    def flush(self, now=None) -> Optional[Capture]:
        return self._maybe_finish(now)


class CrowSerial:
    """Open the Crow USB serial port for reading. Writes go through build_write()."""

    def __init__(self, port: Optional[str] = None, baud: int = BAUD, serial_factory=None):
        self.port = port
        self.baud = baud
        self._serial_factory = serial_factory
        self._ser = None

    def open(self):
        path = self.port or find_port(os.environ.get("EVILCROW_PORT"))
        if not path:
            raise CrowError("absent", "Evil Crow not detected on USB serial")
        if not os.path.exists(path):
            raise CrowError("absent", f"port missing: {path}")
        try:
            if self._serial_factory:
                self._ser = self._serial_factory(path, self.baud)
            else:
                import serial
                self._ser = serial.Serial(
                    port=path, baudrate=self.baud, timeout=0.2, write_timeout=0.2,
                )
        except CrowError:
            raise
        except Exception as e:  # noqa: BLE001
            raise CrowError("busy", f"cannot open {path}: {e}") from e
        self.port = path
        return self

    def close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass
            self._ser = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()

    def readline(self) -> str:
        if self._ser is None:
            raise CrowError("absent", "port not open")
        raw = self._ser.readline()
        if not raw:
            return ""
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")

    def write_command(self, cmd: str) -> None:
        data = build_write(cmd)
        if self._ser is None:
            raise CrowError("absent", "port not open")
        low = data.decode("ascii").lower()
        for pat in FORBIDDEN_WRITE_PATTERNS:
            if pat in low:
                raise CrowError("tx", f"refused forbidden pattern: {pat}")
        self._ser.write(data)
