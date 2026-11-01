"""Tool registry: the registry *is* the allowlist.

Each tool lives in <tools_dir>/<id>/ with manifest.json and an executable named
by manifest "exec" (default "run"). Only tools that load cleanly are exposed.
"""
import json
import logging
import os
import stat

from .schema import ID_RE, PNAME_RE, PVAL_RE, TYPES, clean

log = logging.getLogger("flipper-runner")

MAX_PARAMS = 2
MAX_ENUM = 8
MAX_TIMEOUT = 300


class ManifestError(ValueError):
    pass


class ParamError(ValueError):
    pass


class Tool:
    def __init__(self, path, m):
        self.path = path
        self.id = m["id"]
        self.label = m["label"]
        self.description = m.get("description", "")
        self.timeout = m["timeout"]
        self.confirm = bool(m.get("confirm", False))
        self.output = m["output"]
        self.params = m.get("params", [])
        self.exe = m["_exe"]

    def check_params(self, given):
        """given: dict name->str. Returns argv suffix. Raises ParamError."""
        spec = {p["name"]: p for p in self.params}
        for name in given:
            if name not in spec:
                raise ParamError(f"unknown param {name}")
        argv = []
        for p in self.params:
            name = p["name"]
            if name not in given:
                if "default" in p:
                    val = str(p["default"])
                else:
                    raise ParamError(f"missing param {name}")
            else:
                val = given[name]
            if not PVAL_RE.match(val):
                raise ParamError(f"bad value for {name}")
            if p["type"] == "enum":
                if val not in p["values"]:
                    raise ParamError(f"{name} not in enum")
            else:
                try:
                    iv = int(val, 10)
                except ValueError:
                    raise ParamError(f"{name} not an int")
                if not p["min"] <= iv <= p["max"]:
                    raise ParamError(f"{name} out of bounds")
                val = str(iv)
            argv.append(f"--{name}={val}")
        return argv

    def frames(self):
        out = [f"T|{self.id}|{clean(self.label, 24)}|{int(self.confirm)}|{self.timeout}|"
               f"{self.output}|{clean(self.description, 64)}"]
        for p in self.params:
            if p["type"] == "enum":
                out.append(f"P|{self.id}|{p['name']}|enum|{','.join(p['values'])}|{p.get('default', '')}")
            else:
                out.append(f"P|{self.id}|{p['name']}|int|{p['min']}|{p['max']}|{p.get('default', p['min'])}")
        return out


def _req_str(m, key, limit, optional=False):
    v = m.get(key)
    if v is None and optional:
        return
    if not isinstance(v, str) or not v or len(v) > limit:
        raise ManifestError(f"{key}: string 1..{limit}")


def parse_manifest(tool_dir, m, strict_perms=True):
    if not isinstance(m, dict):
        raise ManifestError("manifest must be an object")
    known = {"id", "label", "description", "timeout", "confirm", "output", "params", "exec", "schema"}
    if set(m) - known:
        raise ManifestError(f"unknown keys {sorted(set(m) - known)}")
    if m.get("schema", 1) != 1:
        raise ManifestError("schema must be 1")
    if not isinstance(m.get("id"), str) or not ID_RE.match(m["id"]):
        raise ManifestError("id must match [a-z0-9_-]{1,24}")
    if m["id"] != os.path.basename(tool_dir):
        raise ManifestError("id must equal directory name")
    _req_str(m, "label", 24)
    _req_str(m, "description", 64, optional=True)
    t = m.get("timeout")
    if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= MAX_TIMEOUT:
        raise ManifestError(f"timeout: int 1..{MAX_TIMEOUT}")
    if not isinstance(m.get("confirm", False), bool):
        raise ManifestError("confirm: bool")
    if m.get("output") not in TYPES:
        raise ManifestError(f"output: one of {TYPES}")
    params = m.get("params", [])
    if not isinstance(params, list) or len(params) > MAX_PARAMS:
        raise ManifestError(f"params: list of <= {MAX_PARAMS}")
    seen = set()
    for p in params:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not PNAME_RE.match(p["name"]):
            raise ManifestError("param name invalid")
        if p["name"] in seen:
            raise ManifestError("duplicate param")
        seen.add(p["name"])
        if p.get("type") == "enum":
            if set(p) - {"name", "type", "values", "default", "label"}:
                raise ManifestError("enum param: unknown keys")
            vals = p.get("values")
            if not isinstance(vals, list) or not 1 <= len(vals) <= MAX_ENUM:
                raise ManifestError(f"enum values: 1..{MAX_ENUM}")
            for v in vals:
                if not isinstance(v, str) or not PVAL_RE.match(v) or len(v) > 16:
                    raise ManifestError("enum value invalid")
            if "default" in p and p["default"] not in vals:
                raise ManifestError("enum default not in values")
        elif p.get("type") == "int":
            if set(p) - {"name", "type", "min", "max", "default", "label"}:
                raise ManifestError("int param: unknown keys")
            lo, hi = p.get("min"), p.get("max")
            for v in (lo, hi):
                if isinstance(v, bool) or not isinstance(v, int) or not -99999 <= v <= 99999:
                    raise ManifestError("int bounds must be ints within +/-99999")
            if lo > hi:
                raise ManifestError("min > max")
            if "default" in p and (not isinstance(p["default"], int) or not lo <= p["default"] <= hi):
                raise ManifestError("int default out of bounds")
        else:
            raise ManifestError("param type must be enum or int (no free-form strings in v1)")
    exe_name = m.get("exec", "run")
    if not isinstance(exe_name, str) or "/" in exe_name or exe_name.startswith("."):
        raise ManifestError("exec must be a plain filename inside the tool dir")
    exe = os.path.join(tool_dir, exe_name)
    try:
        st = os.stat(exe, follow_symlinks=False)
    except FileNotFoundError:
        raise ManifestError("exec not found")
    if not stat.S_ISREG(st.st_mode) or not st.st_mode & stat.S_IXUSR:
        raise ManifestError("exec must be a regular executable file (no symlinks)")
    if strict_perms:
        for path in (tool_dir, exe, os.path.join(tool_dir, "manifest.json")):
            s = os.stat(path, follow_symlinks=False)
            if s.st_uid != 0 or s.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ManifestError(f"{path} must be root-owned and not group/world-writable")
    m = dict(m)
    m["_exe"] = exe
    return Tool(tool_dir, m)


class Registry:
    def __init__(self, tools_dir, strict_perms=True):
        self.tools_dir = os.path.abspath(tools_dir)
        self.strict_perms = strict_perms
        self.tools = {}
        self.reload()

    def reload(self):
        tools = {}
        try:
            names = sorted(os.listdir(self.tools_dir))
        except FileNotFoundError:
            names = []
        for name in names:
            d = os.path.join(self.tools_dir, name)
            if not os.path.isdir(d) or os.path.islink(d):
                continue
            try:
                with open(os.path.join(d, "manifest.json"), "rb") as f:
                    raw = f.read(8192 + 1)
                if len(raw) > 8192:
                    raise ManifestError("manifest too large")
                tool = parse_manifest(d, json.loads(raw), self.strict_perms)
                tools[tool.id] = tool
            except (OSError, ValueError) as e:
                log.warning("skipping tool %s: %s", clean(name, 32), clean(str(e), 120))
            if len(tools) >= 32:
                log.warning("tool limit (32) reached; ignoring the rest")
                break
        self.tools = tools

    def get(self, tool_id):
        return self.tools.get(tool_id)
