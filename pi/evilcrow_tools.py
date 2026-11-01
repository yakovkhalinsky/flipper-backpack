"""Logic for the evilcrow-* tools (receive / monitor only)."""
from __future__ import annotations

import json
import os
import time
from typing import List, Optional

import evilcrow_serial as ec
from flipper_tool import fit, item, kv, list_, progress, status, text

ERR_TITLE = "Evil Crow"
CACHE_FILE = "/run/flipper-runner/evilcrow-index.json"
CACHE_TTL = 300


def err(e: Exception):
    if isinstance(e, ec.CrowError):
        level = "warn" if e.kind in ("absent", "busy") else "error"
        return status(level, str(e), title=ERR_TITLE)
    return status("error", fit(str(e), 96), title=ERR_TITLE)


def _load_captures(path=None) -> List[ec.Capture]:
    path = path or ec.CAPTURES_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            data = json.loads(f.read(256 * 1024))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return []
    out = []
    for d in data["items"]:
        try:
            out.append(ec.Capture.from_dict(d))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _write_index(keys, path=None, now=None):
    path = path or CACHE_FILE
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"t": int(now or time.time()), "keys": keys}, f)
    os.replace(tmp, path)


def _read_index_key(idx, path=None, now=None, ttl=CACHE_TTL):
    path = path or CACHE_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            c = json.loads(f.read(8192))
    except (OSError, ValueError):
        raise ec.CrowError("bad", "Capture list expired; reopen Evil Crow captures")
    if not isinstance(c, dict) or (now or time.time()) - int(c.get("t", 0)) > ttl:
        raise ec.CrowError("bad", "Capture list expired; reopen Evil Crow captures")
    keys = c.get("keys")
    if not isinstance(keys, list) or not 0 <= idx < len(keys):
        raise ec.CrowError("bad", "No such capture in the list")
    k = keys[idx]
    if not isinstance(k, int):
        raise ec.CrowError("bad", "invalid capture key")
    return k


def _age(ts, now):
    d = max(0, int(now) - int(ts))
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    return f"{d // 3600}h"


def status_doc(port=None, captures_path=None, reader_state=None, now=None):
    now = now or time.time()
    env_port = os.environ.get("EVILCROW_PORT")
    path = port if port is not None else ec.find_port(env_port)
    info = ec.port_info(path)
    caps = _load_captures(captures_path)
    last = caps[-1] if caps else None
    pairs = [
        ("Connected", "yes" if info["exists"] else "no"),
        ("Port", fit(info["path"] or "(none)", 40)),
        ("Baud", str(ec.BAUD)),
        ("Firmware", "stock: no serial cmds"),
        ("RX log", "v1.3.x serial / silent on tip"),
        ("Captures", str(len(caps))),
    ]
    if info.get("by_id"):
        pairs.append(("by-id", fit(info["by_id"], 40)))
    if last:
        pairs.append(("Last", fit(f"{_age(last.t, now)} ago {last.summary()}", 40)))
    if reader_state:
        pairs.append(("Reader", fit(reader_state, 40)))
    else:
        # Best-effort: check unit without sudo.
        try:
            import subprocess
            r = subprocess.run(
                ["systemctl", "is-active", "evilcrow-reader.service"],
                capture_output=True, text=True, timeout=2,
            )
            pairs.append(("Reader", fit(r.stdout.strip() or r.stderr.strip() or "?", 40)))
        except Exception:  # noqa: BLE001
            pairs.append(("Reader", "unknown"))
    pairs.append(("Writes", "allowlist empty (RX only)"))
    return kv(pairs[:16], title="Evil Crow status")


def captures_doc(limit=10, captures_path=None, cache_path=None, now=None):
    limit = max(1, min(int(limit), 20))
    now = now or time.time()
    caps = _load_captures(captures_path)
    if not caps:
        tip = ("No captures yet. Current stock firmware does not print RX "
               "on USB serial; see README. Older v1.3.x does.")
        return status("warn", tip, title="Evil Crow captures")
    # Newest first.
    caps = list(reversed(caps))[:limit]
    # Index key = capture timestamp (unique enough in the ring).
    keys = [c.t for c in caps]
    _write_index(keys, cache_path, now)
    items = []
    for i, c in enumerate(caps):
        lab = fit(f"{_age(c.t, now)} {c.summary()}", 32)
        items.append(item(lab, "evilcrow-capture", idx=i))
    return list_(items, title="Evil Crow captures")


def capture_doc(idx, captures_path=None, cache_path=None, now=None):
    now = now or time.time()
    try:
        key = _read_index_key(int(idx), cache_path, now)
    except ec.CrowError as e:
        return err(e)
    caps = {c.t: c for c in _load_captures(captures_path)}
    c = caps.get(key)
    if c is None:
        return status("warn", "Capture gone from ring buffer", title=ERR_TITLE)
    pairs = [
        ("When", time.strftime("%H:%M:%S", time.localtime(c.t))),
        ("Age", _age(c.t, now)),
        ("Samples", str(c.count)),
        ("Symbol", f"{c.symbol_us} us" if c.symbol_us else "?"),
        ("Corrected", str(c.corrected_count) if c.corrected_count is not None else "?"),
        ("Bits", str(len(c.binary)) if c.binary else "0"),
        ("Preview", fit(c.preview(), 40)),
        ("Source", c.source),
        ("Freq", "(set in Crow web UI)"),
        ("Mod", "(set in Crow web UI)"),
        ("RSSI", "(not on serial)"),
    ]
    return kv(pairs[:16], title="Evil Crow capture")


def live_doc(captures_path=None, now=None, ticks=3, sleep_fn=time.sleep):
    """Stream a few progress updates, then a short text summary of the latest capture."""
    now = now or time.time()
    for i in range(max(1, ticks)):
        caps = _load_captures(captures_path)
        last = caps[-1] if caps else None
        label = last.summary() if last else "listening"
        progress(int((i + 1) * 100 / ticks), fit(label, 32))
        if i + 1 < ticks:
            sleep_fn(0.35)
    caps = _load_captures(captures_path)
    if not caps:
        return text([
            "No captures in the ring buffer.",
            "Stock tip firmware is silent on USB serial.",
            "Start RX from the Crow web UI; use v1.3.x for serial logs,",
            "or a receive-only serial logger firmware.",
        ], title="Evil Crow live")
    last = caps[-1]
    lines = [
        f"Last: {_age(last.t, now)} ago",
        f"Samples: {last.count}",
        f"Symbol: {last.symbol_us or '?'} us",
        f"Preview: {last.preview()}",
        f"Buffered: {len(caps)}/{ec.CAPTURES_MAX}",
    ]
    return text(lines, title="Evil Crow live")
