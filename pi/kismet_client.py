"""Read-only client for the local Kismet REST API (used by the kismet-* tools).

Security notes
- Talks to 127.0.0.1:2501 only; the host/port are not configurable from the Flipper.
- The API token is read from /etc/flipper-runner/kismet.env (KISMET_API_KEY=...),
  never from argv or the environment, and is never printed.
- Responses are size-capped before JSON parsing; every string shown on the
  Flipper is truncated to the schema v1 caps by flipper_tool.
- Device detail is addressed by an *index* into a short-lived cache written by
  kismet-devices, not by a free-form key from the Flipper. Cached keys are
  re-validated against KEY_RE before being put in a URL.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:2501"
ENV_FILE = "/etc/flipper-runner/kismet.env"
CACHE_FILE = "/run/flipper-runner/kismet-devices.json"
CACHE_TTL = 300          # seconds a device list stays selectable
LOG_GLOB = "/var/log/kismet/*.kismet*"
LOG_DIR = "/var/log/kismet"
MAX_BODY = 512 * 1024    # bytes read from Kismet per request
TIMEOUT = 4
KEY_RE = re.compile(r"\A[A-F0-9_:]{1,40}\Z")
TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{8,128}\Z")

F = "kismet.device.base."
SIG = F + "signal/kismet.common.signal.last_signal"
DEVICE_FIELDS = [F + "key", F + "macaddr", F + "commonname", F + "name", F + "type",
                 F + "channel", F + "manuf", F + "first_time", F + "last_time",
                 F + "packets.total", SIG]


class KismetError(Exception):
    """kind: down | auth | config | bad | toobig"""
    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind = kind


def load_key(path=None):
    path = path or ENV_FILE
    try:
        with open(path, encoding="utf-8") as f:
            for line in f.read(4096).splitlines():
                line = line.strip()
                if line.startswith("KISMET_API_KEY="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if TOKEN_RE.match(v):
                        return v
                    raise KismetError("config", "API key in kismet.env is malformed")
    except FileNotFoundError:
        raise KismetError("config", "No kismet.env; see README Kismet setup")
    except PermissionError:
        raise KismetError("config", "Cannot read kismet.env (owner/mode?)")
    raise KismetError("config", "KISMET_API_KEY missing in kismet.env")


class Client:
    def __init__(self, base=None, key=None):
        self.base = base or BASE
        self._key = key if key is not None else load_key()

    def _req(self, path, form=None):
        data = None
        if form is not None:
            data = urllib.parse.urlencode({"json": json.dumps(form)}).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Cookie": "KISMET=" + self._key})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = r.read(MAX_BODY + 1)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise KismetError("auth", "Kismet rejected the API key")
            if e.code == 404:
                raise KismetError("bad", "Not found in Kismet")
            raise KismetError("bad", f"Kismet HTTP {e.code}")
        except (urllib.error.URLError, OSError, TimeoutError):
            raise KismetError("down", "Kismet not reachable on 127.0.0.1:2501")
        if len(body) > MAX_BODY:
            raise KismetError("toobig", "Kismet response too large")
        try:
            return json.loads(body)
        except ValueError:
            raise KismetError("bad", "Kismet sent invalid JSON")

    def system_status(self):
        return self._req("/system/status.json")

    def datasources(self):
        r = self._req("/datasource/all_sources.json")
        return r if isinstance(r, list) else []

    def packet_rate(self):
        try:
            r = self._req("/packetchain/packet_stats.json")
            rrd = r.get("kismet.packetchain.packets_rrd", {})
            return int(rrd.get("kismet.common.rrd.last_value", 0))
        except (KismetError, AttributeError, TypeError, ValueError):
            return None

    def devices(self, since=-3600):
        r = self._req(f"/devices/last-time/{int(since)}/devices.json", {"fields": DEVICE_FIELDS})
        return [d for d in r if isinstance(d, dict)] if isinstance(r, list) else []

    def device(self, key):
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise KismetError("bad", "invalid device key")
        r = self._req("/devices/by-key/" + urllib.parse.quote(key, safe="") + "/device.json",
                      {"fields": DEVICE_FIELDS})
        if not isinstance(r, dict):
            raise KismetError("bad", "unexpected device record")
        return r

    def alerts(self):
        r = self._req("/alerts/all_alerts.json")
        return [a for a in r if isinstance(a, dict)] if isinstance(r, list) else []


# ---- helpers -------------------------------------------------------------

def unit_active(unit="kismet"):
    try:
        return subprocess.run(["systemctl", "is-active", unit], capture_output=True,
                              text=True, timeout=4).stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def human_bytes(n):
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def human_secs(s):
    s = max(0, int(s))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d or h:
        return f"{d}d{h}h{m}m" if d else f"{h}h{m}m"
    return f"{m}m{s}s" if m else f"{s}s"


def ago(ts, now=None):
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return "?"
    if ts <= 0:
        return "?"
    return human_secs((now or time.time()) - ts) + " ago"


def mask_mac(mac, mode):
    mac = str(mac or "")
    if mode == "full":
        return mac
    parts = mac.split(":")
    if len(parts) == 6:
        return ":".join(parts[:3] + ["xx"] * 3)  # keep OUI, hide NIC part
    return "masked"


def log_usage(pattern=None, logdir=None):
    size = 0
    for p in glob.glob(pattern or LOG_GLOB):
        try:
            size += os.path.getsize(p)
        except OSError:
            pass
    try:
        free = shutil.disk_usage(logdir or LOG_DIR).free
    except OSError:
        free = shutil.disk_usage("/").free
    return size, free


def sig(d):
    v = d.get(SIG)
    if isinstance(v, dict):
        v = v.get("kismet.common.signal.last_signal")
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if v != 0 else None


def dname(d):
    for k in (F + "commonname", F + "name", F + "macaddr"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "?"


def sort_devices(devs, by):
    if by == "signal":
        return sorted(devs, key=lambda d: (sig(d) is None, -(sig(d) or -999)))
    return sorted(devs, key=lambda d: -int(d.get(F + "last_time") or 0))


def write_cache(keys, path=None, now=None):
    path = path or CACHE_FILE
    keys = [k for k in keys if isinstance(k, str) and KEY_RE.match(k)]
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"t": int(now or time.time()), "keys": keys}, f)
    os.replace(tmp, path)


def read_cache_key(idx, path=None, now=None, ttl=CACHE_TTL):
    path = path or CACHE_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            c = json.loads(f.read(8192))
    except (OSError, ValueError):
        raise KismetError("bad", "Device list expired; reopen Kismet devices")
    if not isinstance(c, dict) or (now or time.time()) - int(c.get("t", 0)) > ttl:
        raise KismetError("bad", "Device list expired; reopen Kismet devices")
    keys = c.get("keys")
    if not isinstance(keys, list) or not 0 <= idx < len(keys):
        raise KismetError("bad", "No such device in the list")
    k = keys[idx]
    if not isinstance(k, str) or not KEY_RE.match(k):
        raise KismetError("bad", "invalid device key")
    return k
