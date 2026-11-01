"""UART protocol server.

Wire: 115200 8N1, ASCII lines ending in \n, max 256 bytes incl. newline.

Flipper -> Pi                       Pi -> Flipper
  HELLO 1                             HELLO|1|schema=1|<version>
  LIST                                T|... P|... (per tool) END|<n>
  DESCRIBE <id>                       T|... P|... END|1
  RUN <id> [name=val ...] [@confirm]  ACK|<id>, PG|pct|label*, BEGIN..DONE, EXIT|<code>|<reason>
  CANCEL                              (running) -> EXIT|-1|cancelled
  PING                                PONG
Errors: ERR|<code>|<message>
"""
import argparse
import json
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time

from . import __version__
from .registry import ParamError, Registry
from .schema import (ID_RE, MAX_FRAME, PNAME_RE, SchemaError, encode, error_view,
                     validate)

log = logging.getLogger("flipper-runner")

PROTO_VERSION = 1
STDOUT_CAP = 16 * 1024
SAFE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
            "FLIPPER_SCHEMA": "1",
            # lets tools `import flipper_tool` from the install dir
            "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}


class Server:
    def __init__(self, rfd, wfd, registry):
        self.rfd, self.wfd = rfd, wfd
        self.reg = registry
        self.wlock = threading.Lock()
        self.hello = False
        self.run_thread = None
        self.proc = None
        self.cancel_evt = threading.Event()
        self.stop = threading.Event()

    # ---- output
    def send(self, frame):
        data = (frame + "\n").encode("ascii", "replace")
        if len(data) > MAX_FRAME:  # never happens with encode(); belt and braces
            data = data[:MAX_FRAME - 1] + b"\n"
        with self.wlock:
            view = memoryview(data)
            while view:
                n = os.write(self.wfd, view)
                view = view[n:]

    def err(self, code, msg):
        self.send(f"ERR|{code}|{msg}")

    # ---- input loop
    def serve(self):
        buf = bytearray()
        discarding = False
        while not self.stop.is_set():
            r, _, _ = select.select([self.rfd], [], [], 0.2)
            if not r:
                continue
            try:
                chunk = os.read(self.rfd, 512)
            except OSError:
                break
            if not chunk:
                break
            for b in chunk:
                if b == 0x0A:
                    if discarding:
                        discarding = False
                    else:
                        self.handle(bytes(buf))
                    buf.clear()
                elif discarding:
                    continue
                elif len(buf) >= MAX_FRAME - 1:
                    buf.clear()
                    discarding = True
                    self.err("frame_too_long", f"max {MAX_FRAME} bytes")
                else:
                    buf.append(b)
        self.cancel()

    def handle(self, raw):
        raw = raw.rstrip(b"\r")
        if not raw:
            return
        try:
            line = raw.decode("ascii")
        except UnicodeDecodeError:
            return self.err("bad_frame", "non-ascii")
        if not line.isprintable():
            return self.err("bad_frame", "non-printable")
        parts = line.split(" ")
        cmd, args = parts[0], parts[1:]
        if cmd == "HELLO":
            if args != [str(PROTO_VERSION)]:
                return self.err("bad_version", f"want HELLO {PROTO_VERSION}")
            self.hello = True
            self.reg.reload()
            return self.send(f"HELLO|{PROTO_VERSION}|schema=1|{__version__}")
        if cmd == "PING":
            return self.send("PONG")
        if not self.hello:
            return self.err("need_hello", "send HELLO 1 first")
        if cmd == "LIST" and not args:
            for t in self.reg.tools.values():
                for f in t.frames():
                    self.send(f)
            return self.send(f"END|{len(self.reg.tools)}")
        if cmd == "DESCRIBE" and len(args) == 1:
            t = self._tool(args[0])
            if t:
                for f in t.frames():
                    self.send(f)
                self.send("END|1")
            return
        if cmd == "RUN" and args:
            return self.start_run(args)
        if cmd == "CANCEL" and not args:
            if not self.busy():
                return self.err("not_running", "nothing to cancel")
            return self.cancel()
        return self.err("bad_command", "unknown command or arguments")

    def _tool(self, tid):
        if not ID_RE.match(tid):
            self.err("bad_id", "invalid tool id")
            return None
        t = self.reg.get(tid)
        if t is None:
            self.err("unknown_tool", tid)
        return t

    def busy(self):
        return self.run_thread is not None and self.run_thread.is_alive()

    # ---- running
    def start_run(self, args):
        if self.busy():
            return self.err("busy", "a tool is already running")
        t = self._tool(args[0])
        if not t:
            return
        confirmed = False
        given = {}
        for a in args[1:]:
            if a == "@confirm":
                confirmed = True
                continue
            name, eq, val = a.partition("=")
            if not eq or not PNAME_RE.match(name) or name in given:
                return self.err("bad_param", "params are name=value")
            given[name] = val
        if t.confirm and not confirmed:
            return self.err("confirm_required", t.id)
        try:
            extra = t.check_params(given)
        except ParamError as e:
            return self.err("bad_param", str(e))
        self.cancel_evt.clear()
        self.send(f"ACK|{t.id}")
        self.run_thread = threading.Thread(target=self._run, args=(t, extra), daemon=True)
        self.run_thread.start()

    def cancel(self):
        self.cancel_evt.set()
        if self.run_thread is not None:
            self.run_thread.join(timeout=10)

    def _kill(self, proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

    def _run(self, t, extra):
        start = time.monotonic()
        argv = [t.exe] + extra
        reason, code = "ok", None
        frames = None
        try:
            proc = subprocess.Popen(argv, cwd=t.path, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    env=SAFE_ENV, start_new_session=True, close_fds=True)
        except OSError as e:
            log.info("run id=%s exit=-1 reason=spawn_failed", t.id)
            self.send_frames(error_view(t.label, f"Could not start: {e.strerror}"))
            return self.send("EXIT|-1|spawn_failed")
        fd = proc.stdout.fileno()
        out = bytearray()
        pending = bytearray()
        deadline = start + t.timeout
        try:
            while True:
                if self.cancel_evt.is_set():
                    reason = "cancelled"
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    reason = "timeout"
                    break
                r, _, _ = select.select([fd], [], [], min(left, 0.1))
                if not r:
                    continue
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    if not self._maybe_progress(line):
                        out += line + b"\n"
                if len(out) + len(pending) > STDOUT_CAP:
                    reason = "too_large"
                    break
        finally:
            if reason != "ok" or proc.poll() is None:
                if reason == "ok":
                    try:
                        proc.wait(timeout=max(0.1, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        reason = "timeout"
                self._kill(proc)
            proc.stdout.close()
        code = proc.returncode
        out += pending
        if reason == "ok":
            frames = self._render(t, bytes(out), code)
        elif reason == "timeout":
            frames = error_view(t.label, f"Timed out after {t.timeout}s")
            code = 124
        elif reason == "too_large":
            frames = error_view(t.label, f"Output exceeded {STDOUT_CAP} bytes")
            code = -1
        else:
            code = -1
        log.info("run id=%s exit=%s reason=%s dur=%.1fs", t.id, code, reason,
                 time.monotonic() - start)
        if frames:
            self.send_frames(frames)
        self.send(f"EXIT|{code}|{reason}")

    def _maybe_progress(self, line):
        s = line.strip()
        if not s.startswith(b"{") or b'"progress"' not in s:
            return False
        try:
            doc = json.loads(s)
            if doc.get("type") != "progress":
                return False
            validate(doc)
        except (ValueError, SchemaError):
            return False
        self.send(encode(doc)[1])  # just the PG frame
        return True

    def _render(self, t, out, code):
        text = out.strip()
        if not text:
            msg = "No output" if code == 0 else f"Failed (exit {code}), no output"
            return error_view(t.label, msg)
        try:
            doc = json.loads(text)
            validate(doc, self.reg)
        except (ValueError, SchemaError) as e:
            log.info("run id=%s invalid_output: %s", t.id, str(e)[:80])
            return error_view(t.label, "Tool output failed schema v1 validation")
        return encode(doc)

    def send_frames(self, frames):
        for f in frames:
            self.send(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Flipper Zero command runner (Pi side)")
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--tools", default="/opt/flipper-runner/tools")
    ap.add_argument("--insecure-perms", action="store_true",
                    help="skip root-ownership checks on tools (development only)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    import serial  # pyserial
    reg = Registry(a.tools, strict_perms=not a.insecure_perms)
    log.info("flipper-runner %s: %d tools from %s", __version__, len(reg.tools), a.tools)
    while True:
        try:
            with serial.Serial(a.port, a.baud, bytesize=8, parity="N", stopbits=1,
                               timeout=0, exclusive=True) as ser:
                Server(ser.fileno(), ser.fileno(), reg).serve()
        except (OSError, serial.SerialException) as e:
            log.warning("serial error: %s; retrying in 3s", e)
        time.sleep(3)


if __name__ == "__main__":
    main()
