"""Output schema v1: validation + compact UART encoding.

Tools print one JSON document (schema v1) to stdout. Optional progress lines
(one JSON object per line, type=progress) may precede it. The service never
forwards raw output: it validates against the caps below and re-encodes into
pipe-separated line frames that the Flipper can parse with a plain split.
"""
import re

SCHEMA_VERSION = 1
MAX_FRAME = 256  # bytes per line on the wire, incl. newline

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}\Z")
PNAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,11}\Z")
PVAL_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,24}\Z")

CAPS = {
    "title": 32,
    "text_lines": 64, "text_len": 64,
    "kv_items": 16, "kv_label": 20, "kv_value": 40,
    "table_cols": 4, "table_rows": 16, "table_cell": 16,
    "status_msg": 96,
    "progress_label": 32,
    "list_items": 20, "list_label": 32,
}
TYPES = ("text", "kv", "table", "status", "progress", "list")
LEVELS = ("ok", "warn", "error")


class SchemaError(ValueError):
    pass


def clean(s, limit):
    """Printable ASCII only, '|' replaced, truncated. Used for wire fields."""
    if not isinstance(s, str):
        s = str(s)
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "|":
            out.append("/")
        elif 0x20 <= o <= 0x7E:
            out.append(ch)
        elif ch in "\t":
            out.append(" ")
        else:
            out.append("?")
    return "".join(out)[:limit]


def _str(v, limit, what):
    if not isinstance(v, str):
        raise SchemaError(f"{what}: not a string")
    if len(v) > limit:
        raise SchemaError(f"{what}: longer than {limit}")
    return v


def _int(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise SchemaError(f"{what}: int {lo}..{hi} required")
    return v


def validate(doc, registry=None):
    """Validate a decoded output document. Returns it, or raises SchemaError.
    registry (optional) is used to check list-item chains against real tools."""
    if not isinstance(doc, dict):
        raise SchemaError("document must be an object")
    if doc.get("schema") != SCHEMA_VERSION:
        raise SchemaError("schema must be 1")
    t = doc.get("type")
    if t not in TYPES:
        raise SchemaError("unknown type")
    allowed = {"schema", "type", "title", "data"}
    if set(doc) - allowed:
        raise SchemaError("unexpected keys")
    if "title" in doc:
        _str(doc["title"], CAPS["title"], "title")
    d = doc.get("data")
    if t == "text":
        if not isinstance(d, list) or len(d) > CAPS["text_lines"]:
            raise SchemaError("text: list of <=64 lines")
        for i, line in enumerate(d):
            _str(line, CAPS["text_len"], f"text[{i}]")
    elif t == "kv":
        if not isinstance(d, list) or len(d) > CAPS["kv_items"]:
            raise SchemaError("kv: list of <=16 pairs")
        for i, p in enumerate(d):
            if not (isinstance(p, list) and len(p) == 2):
                raise SchemaError(f"kv[{i}]: [label, value]")
            _str(p[0], CAPS["kv_label"], f"kv[{i}].label")
            _str(p[1], CAPS["kv_value"], f"kv[{i}].value")
    elif t == "table":
        if not isinstance(d, dict) or set(d) != {"columns", "rows"}:
            raise SchemaError("table: {columns, rows}")
        cols, rows = d["columns"], d["rows"]
        if not isinstance(cols, list) or not 1 <= len(cols) <= CAPS["table_cols"]:
            raise SchemaError("table: 1..4 columns")
        for c in cols:
            _str(c, CAPS["table_cell"], "column")
        if not isinstance(rows, list) or len(rows) > CAPS["table_rows"]:
            raise SchemaError("table: <=16 rows")
        for r in rows:
            if not isinstance(r, list) or len(r) != len(cols):
                raise SchemaError("table: row width mismatch")
            for c in r:
                _str(c, CAPS["table_cell"], "cell")
    elif t == "status":
        if not isinstance(d, dict) or set(d) != {"level", "message"}:
            raise SchemaError("status: {level, message}")
        if d["level"] not in LEVELS:
            raise SchemaError("status: bad level")
        _str(d["message"], CAPS["status_msg"], "status.message")
    elif t == "progress":
        if not isinstance(d, dict) or set(d) != {"pct", "label"}:
            raise SchemaError("progress: {pct, label}")
        _int(d["pct"], 0, 100, "progress.pct")
        _str(d["label"], CAPS["progress_label"], "progress.label")
    elif t == "list":
        if not isinstance(d, list) or len(d) > CAPS["list_items"]:
            raise SchemaError("list: <=20 items")
        for i, it in enumerate(d):
            if not isinstance(it, dict) or not {"label"} <= set(it) <= {"label", "tool", "params"}:
                raise SchemaError(f"list[{i}]: {{label, tool?, params?}}")
            _str(it["label"], CAPS["list_label"], f"list[{i}].label")
            if "tool" in it:
                tool = it["tool"]
                if not isinstance(tool, str) or not ID_RE.match(tool):
                    raise SchemaError(f"list[{i}].tool invalid")
                params = it.get("params", {})
                if not isinstance(params, dict) or len(params) > 1:
                    raise SchemaError(f"list[{i}].params: at most one")
                for k, v in params.items():
                    if not PNAME_RE.match(k) or not isinstance(v, (str, int)) or isinstance(v, bool) \
                            or not PVAL_RE.match(str(v)):
                        raise SchemaError(f"list[{i}].params invalid")
                if registry is not None:
                    target = registry.get(tool)
                    if target is None:
                        raise SchemaError(f"list[{i}]: chains to unknown tool")
                    target.check_params({k: str(v) for k, v in params.items()})
            elif "params" in it:
                raise SchemaError(f"list[{i}]: params without tool")
    return doc


def encode(doc):
    """Validated doc -> list of wire frames (without newline)."""
    t = doc["type"]
    title = clean(doc.get("title", ""), CAPS["title"])
    frames = [f"BEGIN|{t}|{title}"]
    d = doc["data"]
    if t == "text":
        frames += ["L|" + clean(x, CAPS["text_len"]) for x in d]
    elif t == "kv":
        frames += [f"KV|{clean(a, CAPS['kv_label'])}|{clean(b, CAPS['kv_value'])}" for a, b in d]
    elif t == "table":
        frames.append("TH|" + "|".join(clean(c, CAPS["table_cell"]) for c in d["columns"]))
        frames += ["TR|" + "|".join(clean(c, CAPS["table_cell"]) for c in r) for r in d["rows"]]
    elif t == "status":
        frames.append(f"ST|{d['level']}|{clean(d['message'], CAPS['status_msg'])}")
    elif t == "progress":
        frames.append(f"PG|{d['pct']}|{clean(d['label'], CAPS['progress_label'])}")
    elif t == "list":
        for it in d:
            lab = clean(it["label"], CAPS["list_label"])
            if "tool" in it:
                (k, v), = (it.get("params") or {"": ""}).items()
                frames.append(f"LI|{lab}|{it['tool']}|{k}|{v}")
            else:
                frames.append(f"LI|{lab}|||")
    frames.append("DONE")
    for f in frames:
        assert len(f) + 1 <= MAX_FRAME, f
    return frames


def error_view(title, message):
    return [f"BEGIN|error|{clean(title, CAPS['title'])}",
            f"ST|error|{clean(message, CAPS['status_msg'])}", "DONE"]
