"""Protocol tests over a pty pair (the service sees a tty, like /dev/serial0)."""
import json
import os
import select
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import tty
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PI = os.path.dirname(HERE)
sys.path.insert(0, PI)

from flipper_runner.registry import Registry  # noqa: E402
from flipper_runner.schema import validate, SchemaError  # noqa: E402
from flipper_runner import service  # noqa: E402

PY = sys.executable


def make_tool(root, tid, body, output="text", timeout=5, confirm=False, params=None):
    d = os.path.join(root, tid)
    os.makedirs(d)
    m = {"schema": 1, "id": tid, "label": tid.title(), "timeout": timeout,
         "confirm": confirm, "output": output}
    if params:
        m["params"] = params
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump(m, f)
    with open(os.path.join(d, "run"), "w") as f:
        f.write(f"#!{PY}\nimport sys, json, time, os\nfrom flipper_tool import *\n" + textwrap.dedent(body))
    os.chmod(os.path.join(d, "run"), 0o755)


class Base(unittest.TestCase):
    def setUp(self):
        self.tools = tempfile.mkdtemp()
        T = self.tools
        make_tool(T, "text", 'emit(text(["hello", "world\\x07|x"], title="T"))')
        make_tool(T, "kv", 'emit(kv([("Host","pi"),("IP","10.0.0.2")]))', output="kv")
        make_tool(T, "table", 'emit(table(["A","B"], [["1","2"],["3","4"]]))', output="table")
        make_tool(T, "status", 'emit(status("warn","careful"))', output="status")
        make_tool(T, "progress", 'progress(10,"start")\nprogress(90,"almost")\nemit(status("ok","done"))',
                  output="progress")
        make_tool(T, "list", 'emit(list_([item("ssh","pick",svc="ssh"), item("plain")]))', output="list")
        make_tool(T, "pick", 'emit(status("ok", params()["svc"]))', output="status",
                  params=[{"name": "svc", "type": "enum", "values": ["ssh", "cron"]}])
        make_tool(T, "count", 'emit(text([params()["n"]]))',
                  params=[{"name": "n", "type": "int", "min": 1, "max": 10, "default": 3}])
        make_tool(T, "badchain", 'print(json.dumps({"schema":1,"type":"list","data":[{"label":"x","tool":"nope"}]}))',
                  output="list")
        make_tool(T, "invalid", 'print(json.dumps({"schema":1,"type":"kv","data":[["a"]]}))')
        make_tool(T, "notjson", 'print("rm -rf / lol <not json>")')
        make_tool(T, "biglines", 'print(json.dumps({"schema":1,"type":"text","data":["x"]*65}))')
        make_tool(T, "flood", 'sys.stdout.write("A"*40000); sys.stdout.flush(); time.sleep(5)', timeout=10)
        make_tool(T, "sleepy", textwrap.dedent('''
            pid = os.fork()
            if pid == 0:
                open(os.environ["MARK"], "w").write(str(os.getpid())) if "MARK" in os.environ else None
                time.sleep(60)
            time.sleep(60)
            '''), timeout=1)
        make_tool(T, "danger", 'emit(status("ok","boom"))', output="status", confirm=True)
        # broken manifests must be skipped, not loaded
        os.makedirs(os.path.join(T, "freeform"))
        with open(os.path.join(T, "freeform", "manifest.json"), "w") as f:
            json.dump({"id": "freeform", "label": "x", "timeout": 5, "output": "text",
                       "params": [{"name": "cmd", "type": "string"}]}, f)
        shutil.copy(os.path.join(T, "text", "run"), os.path.join(T, "freeform", "run"))

        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        tty.setraw(self.master)
        self.reg = Registry(T, strict_perms=False)
        self.srv = service.Server(self.slave, self.slave, self.reg)
        self.thr = threading.Thread(target=self.srv.serve, daemon=True)
        self.thr.start()
        self.buf = b""

    def tearDown(self):
        self.srv.stop.set()
        self.srv.cancel()
        self.thr.join(2)
        os.close(self.master)
        os.close(self.slave)
        shutil.rmtree(self.tools)

    def tx(self, line):
        os.write(self.master, line.encode() + b"\n")

    def rx(self, timeout=5):
        end = time.time() + timeout
        while b"\n" not in self.buf:
            left = end - time.time()
            if left <= 0:
                raise AssertionError("timeout waiting for frame")
            r, _, _ = select.select([self.master], [], [], left)
            if r:
                self.buf += os.read(self.master, 4096)
        line, _, self.buf = self.buf.partition(b"\n")
        self.assertLessEqual(len(line) + 1, 256)
        return line.decode("ascii")

    def until(self, prefix, timeout=8):
        frames = []
        while True:
            f = self.rx(timeout)
            frames.append(f)
            if f.startswith(prefix):
                return frames

    def hello(self):
        self.tx("HELLO 1")
        self.assertEqual(self.rx(), "HELLO|1|schema=1|1.0.0")

    def run_tool(self, cmd):
        self.tx(cmd)
        return self.until("EXIT|")


class TestProtocol(Base):
    def test_handshake_required_and_version(self):
        self.tx("LIST")
        self.assertTrue(self.rx().startswith("ERR|need_hello"))
        self.tx("HELLO 2")
        self.assertTrue(self.rx().startswith("ERR|bad_version"))
        self.hello()
        self.tx("PING")
        self.assertEqual(self.rx(), "PONG")

    def test_discovery(self):
        self.hello()
        self.tx("LIST")
        frames = self.until("END|")
        ids = [f.split("|")[1] for f in frames if f.startswith("T|")]
        self.assertIn("kv", ids)
        self.assertNotIn("freeform", ids)  # free-form string param rejected
        self.assertEqual(frames[-1], f"END|{len(ids)}")
        self.assertIn("P|pick|svc|enum|ssh,cron|", frames)
        self.assertIn("P|count|n|int|1|10|3", frames)
        danger = [f for f in frames if f.startswith("T|danger|")][0]
        self.assertEqual(danger.split("|")[3], "1")
        self.tx("DESCRIBE kv")
        self.assertEqual(self.until("END|"), ["T|kv|Kv|0|5|kv|", "END|1"])

    def test_text_sanitised(self):
        self.hello()
        fr = self.run_tool("RUN text")
        self.assertEqual(fr, ["ACK|text", "BEGIN|text|T", "L|hello", "L|world?/x", "DONE", "EXIT|0|ok"])

    def test_kv(self):
        self.hello()
        self.assertEqual(self.run_tool("RUN kv")[1:-1],
                         ["BEGIN|kv|", "KV|Host|pi", "KV|IP|10.0.0.2", "DONE"])

    def test_table(self):
        self.hello()
        self.assertEqual(self.run_tool("RUN table")[1:-1],
                         ["BEGIN|table|", "TH|A|B", "TR|1|2", "TR|3|4", "DONE"])

    def test_status(self):
        self.hello()
        self.assertIn("ST|warn|careful", self.run_tool("RUN status"))

    def test_progress_streams_before_result(self):
        self.hello()
        fr = self.run_tool("RUN progress")
        self.assertEqual(fr[:3], ["ACK|progress", "PG|10|start", "PG|90|almost"])
        self.assertIn("ST|ok|done", fr)

    def test_list_and_chain(self):
        self.hello()
        fr = self.run_tool("RUN list")
        self.assertIn("LI|ssh|pick|svc|ssh", fr)
        self.assertIn("LI|plain|||", fr)
        fr = self.run_tool("RUN pick svc=ssh")
        self.assertIn("ST|ok|ssh", fr)

    def test_chain_to_unknown_tool_is_error_view(self):
        self.hello()
        fr = self.run_tool("RUN badchain")
        self.assertEqual(fr[1], "BEGIN|error|Badchain")

    def test_invalid_output_is_error_view(self):
        self.hello()
        for t in ("invalid", "notjson", "biglines"):
            fr = self.run_tool(f"RUN {t}")
            self.assertTrue(fr[1].startswith("BEGIN|error|"), fr)
            self.assertEqual(fr[2], "ST|error|Tool output failed schema v1 validation")
            self.assertFalse(any("rm -rf" in f for f in fr))

    def test_oversize_output_killed(self):
        self.hello()
        t0 = time.time()
        fr = self.run_tool("RUN flood")
        self.assertLess(time.time() - t0, 4)
        self.assertIn("ST|error|Output exceeded 16384 bytes", fr)
        self.assertEqual(fr[-1], "EXIT|-1|too_large")

    def test_param_bounds(self):
        self.hello()
        for bad in ("RUN pick svc=bash", "RUN pick", "RUN pick svc=ssh extra=1",
                    "RUN count n=0", "RUN count n=11", "RUN count n=abc", "RUN count n=1;id",
                    "RUN pick svc", "RUN count n=1 n=2"):
            self.tx(bad)
            self.assertTrue(self.rx().startswith("ERR|bad_param"), bad)
        self.assertIn("L|3", self.run_tool("RUN count"))
        self.assertIn("L|10", self.run_tool("RUN count n=10"))

    def test_timeout_kills_process_group(self):
        mark = os.path.join(self.tools, "child.pid")
        service.SAFE_ENV["MARK"] = mark
        try:
            self.hello()
            t0 = time.time()
            fr = self.run_tool("RUN sleepy")
            self.assertLess(time.time() - t0, 5)
            self.assertIn("ST|error|Timed out after 1s", fr)
            self.assertEqual(fr[-1], "EXIT|124|timeout")
            with open(mark) as f:
                child = int(f.read())
            time.sleep(0.2)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)  # grandchild gone too (reaped by init, or zombie-free)
        finally:
            service.SAFE_ENV.pop("MARK", None)

    def test_cancel(self):
        self.hello()
        self.tx("RUN sleepy")
        self.assertEqual(self.rx(), "ACK|sleepy")
        self.tx("RUN kv")
        self.assertTrue(self.rx().startswith("ERR|busy"))
        self.tx("CANCEL")
        self.assertEqual(self.rx(), "EXIT|-1|cancelled")
        self.tx("CANCEL")
        self.assertTrue(self.rx().startswith("ERR|not_running"))

    def test_unknown_and_bad_ids(self):
        self.hello()
        self.tx("RUN nope")
        self.assertEqual(self.rx(), "ERR|unknown_tool|nope")
        for bad in ("RUN ../etc", "RUN UPPER", "RUN " + "a" * 25, "DESCRIBE /bin/sh"):
            self.tx(bad)
            self.assertTrue(self.rx().startswith("ERR|bad_id"), bad)
        self.tx("RUN echo hi; reboot")  # a command string is just an unknown id
        self.assertEqual(self.rx(), "ERR|unknown_tool|echo")

    def test_oversize_frame_rejected(self):
        self.hello()
        self.tx("RUN " + "a" * 400)
        self.assertTrue(self.rx().startswith("ERR|frame_too_long"))
        self.tx("PING")  # stream resyncs on the next newline
        self.assertEqual(self.rx(), "PONG")

    def test_non_printable_frame_rejected(self):
        self.hello()
        os.write(self.master, b"RUN k\x01v\n")
        self.assertTrue(self.rx().startswith("ERR|bad_frame"))
        os.write(self.master, b"RUN \xffkv\n")
        self.assertTrue(self.rx().startswith("ERR|bad_frame"))

    def test_confirm_enforced(self):
        self.hello()
        self.tx("RUN danger")
        self.assertEqual(self.rx(), "ERR|confirm_required|danger")
        self.assertIn("ST|ok|boom", self.run_tool("RUN danger @confirm"))


class TestSchemaAndManifests(unittest.TestCase):
    def test_caps(self):
        ok = {"schema": 1, "type": "kv", "data": [["a", "b"]]}
        validate(ok)
        bad = [
            {"schema": 2, "type": "kv", "data": []},
            {"schema": 1, "type": "kv", "data": [["a" * 21, "b"]]},
            {"schema": 1, "type": "kv", "data": [["a", "b"]] * 17},
            {"schema": 1, "type": "table", "data": {"columns": ["a"] * 5, "rows": []}},
            {"schema": 1, "type": "table", "data": {"columns": ["a", "b"], "rows": [["1"]]}},
            {"schema": 1, "type": "status", "data": {"level": "panic", "message": "x"}},
            {"schema": 1, "type": "progress", "data": {"pct": 101, "label": "x"}},
            {"schema": 1, "type": "progress", "data": {"pct": True, "label": "x"}},
            {"schema": 1, "type": "list", "data": [{"label": "x", "params": {"a": "b"}}]},
            {"schema": 1, "type": "list", "data": [{"label": "x", "tool": "t", "params": {"a": "b c"}}]},
            {"schema": 1, "type": "text", "data": ["x"], "extra": 1},
            {"schema": 1, "type": "html", "data": []},
        ]
        for b in bad:
            with self.assertRaises(SchemaError, msg=b):
                validate(b)

    def test_shipped_tools_load(self):
        reg = Registry(os.path.join(PI, "tools"), strict_perms=False)
        self.assertEqual(sorted(reg.tools), ["disk", "kismet-alerts", "kismet-device", "kismet-devices",
                                             "kismet-start", "kismet-status", "kismet-stop", "reboot",
                                             "service-status", "services", "shutdown", "sysinfo", "wifi"])
        self.assertTrue(reg.get("reboot").confirm and reg.get("shutdown").confirm)

    def test_services_chain_matches_enum(self):
        import subprocess
        env = dict(os.environ, PYTHONPATH=PI)
        out = subprocess.run([PY, os.path.join(PI, "tools", "services", "run")], env=env,
                             capture_output=True, text=True, check=True).stdout
        doc = json.loads(out)
        validate(doc, Registry(os.path.join(PI, "tools"), strict_perms=False))

    def test_strict_perms_rejects_user_owned_tools(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        self.assertEqual(Registry(os.path.join(PI, "tools"), strict_perms=True).tools, {})


if __name__ == "__main__":
    unittest.main()
