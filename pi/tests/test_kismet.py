"""kismet-* tools against a mocked Kismet REST server on 127.0.0.1."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PI = os.path.dirname(HERE)
sys.path.insert(0, PI)

import kismet_client as kc  # noqa: E402
import kismet_tools as kt  # noqa: E402
from flipper_runner.registry import ParamError, Registry  # noqa: E402
from flipper_runner.schema import encode, validate  # noqa: E402

KEY = "goodtoken_123456"
NOW = 1_800_000_000
F = kc.F


def dev(i, sig=-50, last=NOW - 10, name=None):
    return {F + "key": f"4202770D00000000_{i:012X}", F + "macaddr": f"AA:BB:CC:00:00:{i:02X}",
            F + "commonname": name if name is not None else f"Net{i}", F + "type": "Wi-Fi AP",
            F + "channel": "6", F + "manuf": "Acme", F + "first_time": NOW - 3600,
            F + "last_time": last, F + "packets.total": 100 + i, kc.SIG: sig}


class Mock(BaseHTTPRequestHandler):
    routes = {}
    seen = []

    def log_message(self, *a):
        pass

    def _go(self):
        Mock.seen.append(self.path)
        if self.headers.get("Cookie") != "KISMET=" + KEY:
            self.send_response(401); self.end_headers(); return
        for prefix, body in Mock.routes.items():
            if self.path.startswith(prefix):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(raw)))
                self.end_headers(); self.wfile.write(raw); return
        self.send_response(404); self.end_headers()

    def do_GET(self):
        self._go()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._go()


class KismetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, "cache.json")
        devs = [dev(i, sig=-90 + i, last=NOW - 100 + 3 * i) for i in range(30)]
        Mock.routes = {
            "/system/status.json": {"kismet.system.timestamp.start_sec": NOW - 3725,
                                    "kismet.system.devices.count": 30},
            "/datasource/all_sources.json": [{"kismet.datasource.name": "usbwifi",
                                              "kismet.datasource.running": 1,
                                              "kismet.datasource.channel": "11"}],
            "/packetchain/packet_stats.json": {"kismet.packetchain.packets_rrd":
                                               {"kismet.common.rrd.last_value": 42}},
            "/devices/last-time/": devs,
            "/devices/by-key/": dev(7),
            "/alerts/all_alerts.json": [{"kismet.alert.header": "DEAUTHFLOOD",
                                         "kismet.alert.timestamp": NOW - 60,
                                         "kismet.alert.text": "x" * 500}],
        }
        Mock.seen = []
        self.c = kc.Client(self.base, KEY)

    def check(self, doc):
        validate(doc)
        encode(doc)
        return doc

    # ---- status
    def test_status(self):
        d = self.check(kt.status_doc(self.c, "active", NOW, os.path.join(self.tmp, "*"), self.tmp))
        kv = dict(d["data"])
        self.assertEqual(kv["Service"], "active")
        self.assertEqual(kv["Uptime"], "1h2m")
        self.assertEqual(kv["Devices"], "30")
        self.assertEqual(kv["Packets/s"], "42")
        self.assertEqual(kv["Src usbwifi"], "up ch11")
        self.assertIn("Free disk", kv)

    def test_status_down(self):
        c = kc.Client("http://127.0.0.1:1", KEY)
        d = self.check(kt.status_doc(c, "inactive", NOW, os.path.join(self.tmp, "*"), self.tmp))
        kv = dict(d["data"])
        self.assertEqual(kv["Service"], "inactive")
        self.assertIn("not reachable", kv["API"])

    def test_unauthorised(self):
        bad = kc.Client(self.base, "wrongtoken_9999")
        d = self.check(kt.devices_doc(client=bad, cache_path=self.cache))
        self.assertEqual(d["type"], "status")
        self.assertEqual(d["data"]["level"], "error")
        self.assertIn("rejected", d["data"]["message"])

    def test_down_views(self):
        c = kc.Client("http://127.0.0.1:1", KEY)
        for doc in (kt.devices_doc(client=c, cache_path=self.cache), kt.alerts_doc(c)):
            self.check(doc)
            self.assertEqual(doc["type"], "status")
            self.assertEqual(doc["data"]["level"], "warn")

    # ---- devices
    def test_devices_sorting_limit_and_chain(self):
        d = self.check(kt.devices_doc("signal", 5, self.c, self.cache, NOW))
        self.assertEqual(len(d["data"]), 5)
        self.assertTrue(d["data"][0]["label"].startswith("Net29"))
        self.assertEqual(d["data"][0], {"label": "Net29 -61", "tool": "kismet-device", "params": {"idx": 0}})
        d = self.check(kt.devices_doc("last_seen", 20, self.c, self.cache, NOW))
        self.assertEqual(len(d["data"]), 20)
        reg = Registry(os.path.join(PI, "tools"), strict_perms=False)
        validate(d, reg)  # chains resolve against the real kismet-device manifest

    def test_devices_limit_clamped_and_bad_sort(self):
        d = self.check(kt.devices_doc("last_seen", 999, self.c, self.cache, NOW))
        self.assertEqual(len(d["data"]), 20)
        with self.assertRaises(ValueError):
            kt.devices_doc("rm -rf", 5, self.c, self.cache, NOW)

    def test_devices_oversize_strings_truncated(self):
        Mock.routes["/devices/last-time/"] = [dev(1, name="S" * 500)]
        d = self.check(kt.devices_doc("signal", 5, self.c, self.cache, NOW))
        self.assertLessEqual(len(d["data"][0]["label"]), 32)

    def test_devices_drop_bad_keys(self):
        bad = dev(1); bad[F + "key"] = "../../etc/passwd"
        Mock.routes["/devices/last-time/"] = [bad, dev(2)]
        d = self.check(kt.devices_doc("signal", 5, self.c, self.cache, NOW))
        self.assertEqual(len(d["data"]), 1)
        self.assertEqual(kc.read_cache_key(0, self.cache, NOW), dev(2)[F + "key"])

    def test_oversize_body_rejected(self):
        Mock.routes["/devices/last-time/"] = b"[" + b" " * (kc.MAX_BODY + 10) + b"]"
        d = self.check(kt.devices_doc(client=self.c, cache_path=self.cache))
        self.assertEqual(d["data"]["level"], "error")
        self.assertIn("too large", d["data"]["message"])

    # ---- device detail
    def test_device_detail_masked_default(self):
        kt.devices_doc("signal", 5, self.c, self.cache, NOW)
        d = self.check(kt.device_doc(0, client=self.c, cache_path=self.cache, now=NOW))
        kv = dict(d["data"])
        self.assertEqual(kv["MAC"], "AA:BB:CC:xx:xx:xx")
        self.assertEqual(kv["Signal"], "-50 dBm")
        self.assertEqual(kv["Packets"], "107")
        self.assertEqual(kv["Last seen"], "10s ago")
        self.assertTrue(any(p.startswith("/devices/by-key/4202770D00000000_") for p in Mock.seen))

    def test_device_detail_full_mac(self):
        kt.devices_doc("signal", 5, self.c, self.cache, NOW)
        d = self.check(kt.device_doc(0, "full", self.c, self.cache, NOW))
        self.assertEqual(dict(d["data"])["MAC"], "AA:BB:CC:00:00:07")
        with self.assertRaises(ValueError):
            kt.device_doc(0, "raw", self.c, self.cache, NOW)

    def test_device_cache_expired_or_missing_or_out_of_range(self):
        d = self.check(kt.device_doc(0, client=self.c, cache_path=self.cache, now=NOW))
        self.assertIn("expired", d["data"]["message"])
        kt.devices_doc("signal", 3, self.c, self.cache, NOW)
        d = self.check(kt.device_doc(0, client=self.c, cache_path=self.cache, now=NOW + kc.CACHE_TTL + 1))
        self.assertIn("expired", d["data"]["message"])
        d = self.check(kt.device_doc(5, client=self.c, cache_path=self.cache, now=NOW))
        self.assertIn("No such device", d["data"]["message"])

    def test_tampered_cache_key_rejected(self):
        with open(self.cache, "w") as f:
            json.dump({"t": NOW, "keys": ["../../admin", "4202770D_ab"]}, f)
        Mock.seen = []
        for i in (0, 1):  # path traversal, lowercase
            d = self.check(kt.device_doc(i, client=self.c, cache_path=self.cache, now=NOW))
            self.assertIn("invalid device key", d["data"]["message"])
        self.assertEqual(Mock.seen, [])

    def test_key_regex(self):
        for k in ("4202770D00000000_AABBCC001122", "AA:BB", "A" * 40):
            self.assertTrue(kc.KEY_RE.match(k))
        for k in ("", "A" * 41, "aa", "A/B", "A%2F", "A B", "A\n", "A?x=1"):
            self.assertFalse(kc.KEY_RE.match(k), k)
            with self.assertRaises(kc.KismetError):
                self.c.device(k)

    # ---- alerts
    def test_alerts_truncated(self):
        d = self.check(kt.alerts_doc(self.c, NOW))
        self.assertEqual(d["type"], "text")
        self.assertIn("DEAUTHFLOOD", d["data"][0])
        self.assertTrue(all(len(x) <= 64 for x in d["data"]))
        Mock.routes["/alerts/all_alerts.json"] = []
        self.assertEqual(self.check(kt.alerts_doc(self.c, NOW))["type"], "status")

    # ---- manifests + params (service-side validation)
    def test_manifests_and_params(self):
        reg = Registry(os.path.join(PI, "tools"), strict_perms=False)
        for t in ("kismet-start", "kismet-stop", "kismet-status", "kismet-devices",
                  "kismet-device", "kismet-alerts"):
            self.assertIsNotNone(reg.get(t), t)
        self.assertTrue(reg.get("kismet-start").confirm and reg.get("kismet-stop").confirm)
        devs, one = reg.get("kismet-devices"), reg.get("kismet-device")
        self.assertEqual(devs.check_params({}), ["--sort=last_seen", "--limit=10"])
        self.assertEqual(one.check_params({"idx": "3"}), ["--idx=3", "--mac=masked"])
        for bad in ({"sort": "name"}, {"limit": "0"}, {"limit": "21"}, {"limit": "x"}):
            with self.assertRaises(ParamError):
                devs.check_params(bad)
        for bad in ({"idx": "20"}, {"idx": "-1"}, {"idx": "4202770D00000000_AA"},
                    {"key": "AA:BB"}, {"mac": "raw"}):
            with self.assertRaises(ParamError):
                one.check_params(bad)


class KeyFileTest(unittest.TestCase):
    def test_load_key(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "k.env")
        with self.assertRaises(kc.KismetError):
            kc.load_key(p)
        open(p, "w").write("# c\nKISMET_API_KEY='abcdEFGH1234'\n")
        self.assertEqual(kc.load_key(p), "abcdEFGH1234")
        open(p, "w").write("KISMET_API_KEY=bad key; rm\n")
        with self.assertRaises(kc.KismetError):
            kc.load_key(p)


if __name__ == "__main__":
    unittest.main()
