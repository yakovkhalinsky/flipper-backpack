"""Helper for flipper-runner tool authors (output schema v1).

    from flipper_tool import kv, emit, params
    p = params()                      # {'svc': 'ssh'} from --name=value argv
    emit(kv([("Host", "pi")], title="System"))

emit() validates locally with the same rules as the service, so a mistake shows
up when you run the tool by hand instead of as an error view on the Flipper.
"""
import json
import sys

try:
    from flipper_runner.schema import validate, SchemaError, CAPS  # installed alongside
except ImportError:  # pragma: no cover
    validate = None
    CAPS = {}

SCHEMA = 1


def _doc(t, data, title=None):
    d = {"schema": SCHEMA, "type": t, "data": data}
    if title:
        d["title"] = title
    return d


def fit(s, n):
    """Truncate a string to n characters (use with CAPS limits)."""
    s = str(s)
    return s if len(s) <= n else s[: max(0, n - 1)] + "~"


def text(lines, title=None):
    return _doc("text", [fit(x, 64) for x in lines][:64], title)


def kv(pairs, title=None):
    return _doc("kv", [[fit(a, 20), fit(b, 40)] for a, b in pairs][:16], title)


def table(columns, rows, title=None):
    return _doc("table", {"columns": [fit(c, 16) for c in columns],
                          "rows": [[fit(c, 16) for c in r] for r in rows][:16]}, title)


def status(level, message, title=None):
    return _doc("status", {"level": level, "message": fit(message, 96)}, title)


def item(label, tool=None, **param):
    it = {"label": fit(label, 32)}
    if tool:
        it["tool"] = tool
        if param:
            it["params"] = param
    return it


def list_(items, title=None):
    return _doc("list", list(items)[:20], title)


def progress(pct, label):
    """Stream a progress update immediately (before the final emit())."""
    sys.stdout.write(json.dumps(_doc("progress", {"pct": int(pct), "label": fit(label, 32)}),
                                separators=(",", ":")) + "\n")
    sys.stdout.flush()


def emit(doc):
    if validate is not None:
        validate(doc)
    sys.stdout.write(json.dumps(doc, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def params(argv=None):
    """Parse --name=value arguments passed by the service (already validated)."""
    out = {}
    for a in (sys.argv[1:] if argv is None else argv):
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            out[k] = v
    return out
