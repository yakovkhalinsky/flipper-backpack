"""Background USB-serial reader for Evil Crow RF V2 (receive only).

Reads lines from the Crow, parses v1.3.x-style RX dumps, and keeps a bounded
ring of captures under /run/flipper-runner/evilcrow-captures.json. Never writes
to the serial port.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

import evilcrow_serial as ec

STOP = False


def _stop(*_a):
    global STOP
    STOP = True


def save_ring(items, path, max_items=ec.CAPTURES_MAX):
    items = items[-max_items:]
    payload = {"t": int(time.time()), "items": [c.to_dict() for c in items]}
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, path)
    return items


def load_ring(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            data = json.loads(f.read(256 * 1024))
        return [ec.Capture.from_dict(d) for d in data.get("items", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def run_loop(port=None, baud=ec.BAUD, out_path=None, serial_factory=None,
             idle_sleep=0.2, max_idle_opens=None):
    """Open (and reopen) the port, parse forever until STOP. Returns capture count."""
    out_path = out_path or ec.CAPTURES_FILE
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    items = load_ring(out_path)
    parser = ec.CaptureParser()
    seen = 0
    opens = 0
    while not STOP:
        path = port or ec.find_port(os.environ.get("EVILCROW_PORT"))
        if not path:
            time.sleep(idle_sleep)
            opens += 1
            if max_idle_opens is not None and opens >= max_idle_opens:
                break
            continue
        try:
            crow = ec.CrowSerial(port=path, baud=baud, serial_factory=serial_factory)
            crow.open()
        except ec.CrowError:
            time.sleep(idle_sleep)
            opens += 1
            if max_idle_opens is not None and opens >= max_idle_opens:
                break
            continue
        opens = 0
        try:
            while not STOP:
                line = crow.readline()
                if line == "":
                    # timeout — keep listening
                    continue
                cap = parser.feed_line(line)
                if cap is not None:
                    items.append(cap)
                    items = save_ring(items, out_path)
                    seen += 1
        finally:
            crow.close()
            leftover = parser.flush()
            if leftover is not None:
                items.append(leftover)
                items = save_ring(items, out_path)
                seen += 1
        time.sleep(idle_sleep)
    return seen


def main(argv=None):
    p = argparse.ArgumentParser(description="Evil Crow RF V2 receive-only serial reader")
    p.add_argument("--port", default=None)
    p.add_argument("--baud", type=int, default=ec.BAUD)
    p.add_argument("--out", default=ec.CAPTURES_FILE)
    args = p.parse_args(argv)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    # Hard rule: this process must never write to the Crow.
    assert len(ec.ALLOWED_WRITES) == 0, "reader must stay receive-only"
    n = run_loop(port=args.port, baud=args.baud, out_path=args.out)
    print(f"evilcrow-reader stopped; captures_seen={n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
