"""Evil Crow RF V2 receive-only tools: parser, ring buffer, allowlist, pty."""
from __future__ import annotations

import glob
import os
import pty
import re
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PI = os.path.dirname(HERE)
sys.path.insert(0, PI)

import evilcrow_reader as er  # noqa: E402
import evilcrow_serial as ec  # noqa: E402
import evilcrow_tools as et  # noqa: E402
from flipper_runner.schema import encode, validate  # noqa: E402

SAMPLE_RX = (
    "Count=12\n"
    "120,340,120,340,120,340,120,340,120,340,120,340,\n"
    "101010101010\n"
    " [Pause: 5000 samples]\n"
    "Samples/Symbol: 340\n"
    "\n"
    "Rawdata corrected:\n"
    "Count=9\n"
    "120,340,120,340,120,340,120,340,\n"
    "-------------------------------------------------------\n"
)

TX_COMMANDS = [
    "settx", "setTX 433.92", "setjammer", "set_jammer", "stopjammer",
    "txconfig", "txprotocol", "txbinary", "txtesla",
    "jammer", "jam_start", "replay", "brute", "bruteforce",
    "rolling", "rawtx", "raw_tx", "transmit", "tx_start",
    "tx_stop", "send_raw", "pushbutton", "board_id_read",
    "rx_start", "set_freq 433920000",
]


class ParserTest(unittest.TestCase):
    def test_parses_v13_block(self):
        p = ec.CaptureParser()
        caps = []
        for line in SAMPLE_RX.splitlines():
            c = p.feed_line(line, now=1_700_000_000)
            if c:
                caps.append(c)
        c = p.flush(now=1_700_000_000)
        if c:
            caps.append(c)
        self.assertEqual(len(caps), 1)
        cap = caps[0]
        self.assertEqual(cap.count, 12)
        self.assertEqual(cap.symbol_us, 340)
        self.assertTrue(cap.binary.startswith("10101010"))
        self.assertGreaterEqual(len(cap.samples_us), 8)
        self.assertTrue(cap.preview().startswith("10101010"))
        self.assertEqual(cap.corrected_count, 9)

    def test_two_blocks(self):
        p = ec.CaptureParser()
        caps = []
        blob = SAMPLE_RX + "\nCount=4\n100,200,100,200,\n\n"
        for line in blob.splitlines():
            c = p.feed_line(line, now=100)
            if c:
                caps.append(c)
        c = p.flush(now=100)
        if c:
            caps.append(c)
        self.assertEqual(len(caps), 2)
        self.assertEqual(caps[1].count, 4)


class AllowlistTest(unittest.TestCase):
    def test_allowlist_empty(self):
        self.assertEqual(len(ec.ALLOWED_WRITES), 0)

    def test_build_write_refuses_everything(self):
        for cmd in TX_COMMANDS + ["hello", "status", ""]:
            with self.assertRaises(ec.CrowError) as cm:
                ec.build_write(cmd)
            self.assertEqual(cm.exception.kind, "tx")

    def test_forbidden_patterns_cover_tx_vocab(self):
        joined = " ".join(ec.FORBIDDEN_WRITE_PATTERNS)
        for must in ("settx", "setjammer", "jammer", "replay", "brute", "transmit", "rawtx"):
            self.assertIn(must, joined)

    def test_write_command_refuses_on_open_port(self):
        class Fake:
            def __init__(self, *a, **k):
                self.written = b""
            def readline(self):
                return b""
            def write(self, data):
                self.written += data
            def close(self):
                pass

        with tempfile.NamedTemporaryFile() as tf:
            crow = ec.CrowSerial(port=tf.name, serial_factory=lambda p, b: Fake())
            crow.open()
            with self.assertRaises(ec.CrowError):
                crow.write_command("settx")
            with self.assertRaises(ec.CrowError):
                crow.write_command("rx_start")
            crow.close()


class SourceGrepTest(unittest.TestCase):
    """Grep-style: tool sources must not contain TX/replay/jam call sites."""

    # evilcrow_serial.py holds the deny-list vocabulary on purpose; grep the
    # tools and reader/tools glue instead.
    ROOTS = [
        os.path.join(PI, "evilcrow_tools.py"),
        os.path.join(PI, "evilcrow_reader.py"),
    ] + sorted(glob.glob(os.path.join(PI, "tools", "evilcrow-*", "run")))

    BAD = re.compile(
        r"(?ix)\b(settx|setjammer|stopjammer|tx_start|tx_stop|rawtx|raw_tx|"
        r"transmit|replay|bruteforce|brute_force|rolling.?code)\b"
    )

    def test_no_tx_call_sites_in_tools(self):
        for path in self.ROOTS:
            text = open(path, encoding="utf-8").read()
            scrubbed = []
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("#"):
                    continue
                if s.startswith('"""') or s.startswith("'''"):
                    continue
                if "FORBIDDEN_WRITE_PATTERNS" in line or "ALLOWED_WRITES" in line:
                    continue
                if line.strip().startswith('"') and any(
                    p in line for p in (
                        "settx", "jammer", "replay", "transmit", "brute", "rawtx", "rolling",
                    )
                ):
                    continue
                if "refused forbidden" in line or "receive-only" in line or "RX only" in line:
                    continue
                if "no serial cmds" in line or "Crow web UI" in line:
                    continue
                scrubbed.append(line)
            body = "\n".join(scrubbed)
            m = self.BAD.search(body)
            self.assertIsNone(m, f"TX-like token in {path}: {m.group(0) if m else ''}")


class RingAndToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.caps = os.path.join(self.tmp.name, "caps.json")
        self.idx = os.path.join(self.tmp.name, "idx.json")
        now = 1_700_000_100
        items = []
        p = ec.CaptureParser()
        for line in SAMPLE_RX.splitlines():
            c = p.feed_line(line, now=now)
            if c:
                caps_c = c
                items.append(c)
        c = p.flush(now=now)
        if c:
            items.append(c)
        items.insert(0, ec.Capture(
            t=now - 60, count=4, samples_us=[100, 200], symbol_us=100, binary="01" * 8))
        er.save_ring(items, self.caps)

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_absent(self):
        doc = et.status_doc(
            port="/no/such/port", captures_path=self.caps,
            reader_state="inactive", now=1_700_000_200)
        validate(doc)
        encode(doc)
        self.assertEqual(doc["type"], "kv")
        vals = dict(doc["data"])
        self.assertEqual(vals["Connected"], "no")
        self.assertEqual(vals["Captures"], "2")

    def test_captures_and_detail(self):
        doc = et.captures_doc(
            limit=10, captures_path=self.caps, cache_path=self.idx, now=1_700_000_200)
        validate(doc)
        encode(doc)
        self.assertEqual(doc["type"], "list")
        self.assertGreaterEqual(len(doc["data"]), 1)
        self.assertEqual(doc["data"][0]["tool"], "evilcrow-capture")
        d2 = et.capture_doc(
            0, captures_path=self.caps, cache_path=self.idx, now=1_700_000_200)
        validate(d2)
        encode(d2)
        self.assertEqual(d2["type"], "kv")
        d3 = et.capture_doc(
            0, captures_path=self.caps, cache_path=self.idx, now=1_700_000_200 + 400)
        self.assertEqual(d3["type"], "status")

    def test_live(self):
        doc = et.live_doc(
            captures_path=self.caps, now=1_700_000_200, ticks=2,
            sleep_fn=lambda _s: None)
        validate(doc)
        encode(doc)
        self.assertEqual(doc["type"], "text")

    def test_live_empty(self):
        empty = os.path.join(self.tmp.name, "empty.json")
        er.save_ring([], empty)
        doc = et.live_doc(
            captures_path=empty, now=1, ticks=1, sleep_fn=lambda _s: None)
        validate(doc)
        self.assertEqual(doc["type"], "text")


class PtyReaderTest(unittest.TestCase):
    def test_reader_consumes_sample(self):
        master, slave = pty.openpty()
        slave_name = os.ttyname(slave)
        out = tempfile.NamedTemporaryFile(delete=False)
        out.close()

        class Factory:
            def __init__(self, path, baud):
                import serial
                self.ser = serial.Serial(port=path, baudrate=baud, timeout=0.2)

            def readline(self):
                return self.ser.readline()

            def write(self, data):
                raise AssertionError("reader must not write")

            def close(self):
                self.ser.close()

        def pump():
            time.sleep(0.1)
            os.write(master, SAMPLE_RX.encode())
            time.sleep(0.4)
            er.STOP = True

        er.STOP = False
        t = threading.Thread(target=pump)
        t.start()
        try:
            n = er.run_loop(
                port=slave_name, out_path=out.name,
                serial_factory=lambda p, b: Factory(p, b),
                idle_sleep=0.05, max_idle_opens=50,
            )
        finally:
            er.STOP = True
            t.join(timeout=2)
            os.close(master)
            try:
                os.close(slave)
            except OSError:
                pass
        self.assertGreaterEqual(n, 1)
        items = er.load_ring(out.name)
        self.assertGreaterEqual(len(items), 1)
        self.assertEqual(items[-1].count, 12)
        os.unlink(out.name)


class RegistryLoadTest(unittest.TestCase):
    def test_manifests_load(self):
        from flipper_runner.registry import Registry
        reg = Registry(os.path.join(PI, "tools"), strict_perms=False)
        for tid in ("evilcrow-status", "evilcrow-captures", "evilcrow-capture", "evilcrow-live"):
            self.assertIsNotNone(reg.get(tid), tid)


if __name__ == "__main__":
    unittest.main()
