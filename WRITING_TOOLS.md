# Writing a tool

A tool is a directory `/opt/flipper-runner/tools/<id>/` containing:

* `manifest.json` — what the Flipper shows and what the service will accept
* `run` — an executable (any language; Python + `flipper_tool` is easiest)

Installed tools **are** the allowlist. The service runs `run` directly with an argv list
(never a shell), as the unprivileged `flipper-runner` user, in its own process group,
with a minimal environment, stdin closed and stderr discarded.

## manifest.json
```json
{
  "schema": 1,
  "id": "service-status",            // [a-z0-9_-], ≤24, must equal the dir name
  "label": "Service status",         // ≤24, menu text
  "description": "systemctl state",  // ≤64, optional
  "timeout": 10,                     // 1..300 s; whole process group killed after this
  "confirm": false,                  // true → Yes/No dialog on the Flipper, and the Pi demands it
  "output": "status",                // text | kv | table | status | progress | list
  "exec": "run",                     // optional, plain filename in the dir
  "params": [                        // ≤2; enum or int only (no free-form strings in v1)
    {"name": "svc", "type": "enum", "values": ["ssh", "cron"], "default": "ssh"},
    {"name": "lines", "type": "int", "min": 1, "max": 50, "default": 10}
  ]
}
```
Params arrive as `--name=value` argv entries, already validated (enum: ≤8 values of
`[A-Za-z0-9_.@-]{1,16}`; int: within min..max). On the Flipper enums are a submenu and ints
a number input. (JSON has no comments — strip the `//` notes above.)

Tools with an invalid manifest are skipped and logged. In production every file and the
directory must be **root-owned and not group/world-writable**, otherwise the tool is skipped,
so the service user can never rewrite what it runs.

### Selecting something that isn't an enum (e.g. a device)
Schema v1 has no free-form string params, on purpose. To drill into items whose ids aren't
known in advance, have the list tool cache the real ids in `/run/flipper-runner/` (a 0700
tmpfs dir owned by the service; write files 0600 with `O_NOFOLLOW`, include a timestamp) and
chain each item with a bounded int index. The detail tool reads the cache, rejects stale
entries, and re-validates the id with a strict `\A...\Z` regex before using it.
See `kismet_tools.py` (`kismet-devices` → `kismet-device`).

## Output: schema v1
Print **one JSON document** to stdout (`schema/output-v1.schema.json`):

| type | data | caps |
|---|---|---|
| `text` | `["line", ...]` | ≤64 lines × ≤64 chars |
| `kv` | `[["label","value"], ...]` | ≤16 pairs, label ≤20, value ≤40 |
| `table` | `{"columns":[...], "rows":[[...],...]}` | ≤4 cols, ≤16 rows, cell ≤16 |
| `status` | `{"level":"ok\|warn\|error","message":"..."}` | message ≤96 |
| `progress` | `{"pct":0-100,"label":"..."}` | label ≤32 |
| `list` | `[{"label":..., "tool":"id", "params":{"name":"value"}}, ...]` | ≤20 items, label ≤32, ≤1 param |

Optional `"title"` (≤32). Before the final document you may stream progress lines (one
JSON object per line, `type: progress`); each is forwarded immediately as a progress bar.
List items with `tool` chain into that tool with the given param; the target must exist
and the param must pass its bounds, or the whole output is rejected.

Anything else — bad JSON, unknown keys, exceeded caps, >16 KB of stdout — is replaced by
an **error view** ("Tool output failed schema v1 validation"). Exit code is shown alongside.

## Helper module
```python
#!/usr/bin/python3
from flipper_tool import kv, table, text, status, list_, item, progress, emit, params

p = params()                         # {'svc': 'ssh'}
progress(20, "checking")             # optional streaming update
emit(kv([("Service", p["svc"]), ("State", "active")], title="Service"))
emit(list_([item("ssh", "service-status", svc="ssh")]))   # chaining example
```
Helpers truncate strings to the caps; `emit()` validates with the service's own rules, so
run your tool by hand first: `PYTHONPATH=/opt/flipper-runner ./run --svc=ssh`.

## Install a tool
```bash
sudo install -d -o root -g root -m 755 /opt/flipper-runner/tools/mytool
sudo install -o root -g root -m 644 manifest.json /opt/flipper-runner/tools/mytool/
sudo install -o root -g root -m 755 run /opt/flipper-runner/tools/mytool/
```
The registry reloads on every `HELLO`, so pick **[Refresh]** on the Flipper — no restart.

## Privileged actions
Don't. If a tool truly needs root, add one exact command line to
`/etc/sudoers.d/flipper-runner` (validate with `sudo visudo -c`) and call it with
`sudo -n /full/path args`, as `reboot`/`shutdown` do. Never use wildcards there.
See `examples/git-pull/` for a disabled example tool.
