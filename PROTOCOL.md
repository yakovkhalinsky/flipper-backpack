# Wire protocol v1 (Flipper ⇄ Pi)

UART 115200 8N1 on Flipper pins 13 (TX) / 14 (RX) ⇄ Pi `/dev/serial0`.
Frames are printable-ASCII lines ending in `\n`, **max 256 bytes including the newline**.
Both sides drop oversize or non-printable frames (the Pi answers `ERR|frame_too_long` /
`ERR|bad_frame`; the Flipper silently drops boot noise).

## Flipper → Pi (space separated)
| Frame | Notes |
|---|---|
| `HELLO 1` | required first; anything else gets `ERR|need_hello` |
| `LIST` | all tool manifests |
| `DESCRIBE <id>` | one manifest |
| `RUN <id> [name=value ...] [@confirm]` | ids `[a-z0-9_-]{1,24}`, values `[A-Za-z0-9_.@-]{1,24}`, checked against the manifest's enum/int bounds |
| `CANCEL` | kills the running tool's process group |
| `PING` | → `PONG` |

There is no way to send a command string: the Pi only resolves ids from its tool registry.
`@confirm` is required for `confirm: true` tools (`ERR|confirm_required` otherwise) — a guard
against stray frames, not authentication (anyone holding the UART is trusted).

## Pi → Flipper (pipe separated; `|` never appears inside a field)
| Frame | Meaning |
|---|---|
| `HELLO|1|schema=1|<ver>` | handshake reply |
| `T|id|label|confirm(0/1)|timeout_s|output|description` | tool |
| `P|id|name|enum|v1,v2,...|default` / `P|id|name|int|min|max|default` | tool param |
| `END|<n>` | end of LIST/DESCRIBE |
| `ACK|id` | run started |
| `PG|pct|label` | progress (streamed while running) |
| `BEGIN|type|title` … `DONE` | result view; type ∈ text, kv, table, status, progress, list, error |
| `L|line` · `KV|label|value` · `TH|c…` / `TR|c…` · `ST|level|msg` · `LI|label|tool|param|value` | view bodies |
| `EXIT|code|reason` | reason ∈ ok, timeout (code 124), too_large, cancelled, spawn_failed |
| `ERR|code|message` | need_hello, bad_version, bad_command, bad_id, unknown_tool, bad_param, confirm_required, busy, not_running, frame_too_long, bad_frame |

## Why line/pipe frames rather than JSON on the wire
The Flipper has ~100 KB of free heap and no JSON parser in the SDK. With fixed tags,
bounded field counts and a 256-byte frame cap it parses with one `split('|')`, in constant
memory, with no recursion or escaping rules to get wrong. All the hard validation
(JSON Schema caps, chain targets, param bounds) happens on the Pi in Python, which then
re-encodes only known fields with sanitised printable ASCII. Raw tool output never
reaches the Flipper: invalid or oversize output becomes a `BEGIN|error` view.
