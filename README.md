# grok-delegate

A local bridge that lets a [Codex](https://github.com/openai/codex) session start
[Grok](https://x.ai) workers on this machine.

Codex is the coordinator. This process is the parent of each `grok` child. A
context is the worker's working directory. The same directory stays busy while
its child is alive. A different directory runs at the same time. There is no
fixed worker count.

This is an unofficial local tool. It is not part of Codex or Grok.

## What it runs

One Python process speaks [MCP](https://modelcontextprotocol.io) over
streamable HTTP on `127.0.0.1:7391`. Codex is a client of that process. The
process starts `grok` with a prompt file, records the pid, and reaps the child
on `SIGCHLD`.

```text
Codex  --MCP-->  mcp_server.py  --child-->  grok --cwd <task directory>
                     |
                     +-- pool.json     context, pid, session id
                     +-- jobs/         one file per delegate call
                     +-- results/      the worker's final JSON
                     +-- bus/          coordinator and worker queues
```

The standard library is the only dependency. Python 3.11 or newer.

## Tools

| Tool | What it does |
|---|---|
| `delegate` | Start a Grok worker. `wait: false` returns a job id. `wait: true` waits for that job without blocking the next MCP call. |
| `delegate_wait` | Read a background job. Returns `running`, or the final text. |
| `pool` | Contexts this process owns, and whether each pid is alive. |
| `bus_publish` | Send a message to one context. |
| `bus_receive` | Read messages workers sent the coordinator. |
| `bus_streams` | Status, job, and last message for every context. |

Omit `agent` on `delegate`. The `cwd` is the context. Pass `agent` only to
resume a context id that `pool` already returned, such as `w3`.

Workers talk back through `bus.py`:

```bash
python3 bus.py pub --as w3 --to coord --kind message --text "started"
python3 bus.py sub --as w3
```

`waiting` means the worker needs a decision. `result` is its final reply.

## Run it

```bash
python3 mcp_server.py --serve
# grok_delegate listening on http://127.0.0.1:7391/mcp
```

The server refuses to bind anything except `127.0.0.1` or `localhost`. A second
start exits if that same command is already running.

Point Codex at it with the fragment in `examples/codex.config.toml`. Restart
Codex after editing `~/.codex/config.toml`. Guidance you can paste into a
project `AGENTS.md` is in `examples/AGENTS.md`.

`examples/agent-routing.json` is a project hint for the coordinator. This
server does not read that file.

## State

By default, state lives in `~/.codex/grok-delegate/`, separate from this
source tree:

| Path | Contents |
|---|---|
| `pool.json` | Context index |
| `jobs/` | Job records |
| `results/` | Worker output |
| `bus/` | Pub/sub queues and stream snapshots |
| `delegates/` | Older pin files. Imported once into the pool. |
| `serve.pid` | Pid of `mcp_server.py --serve` |

| Variable | Default |
|---|---|
| `GROK_DELEGATE_STATE` | `~/.codex/grok-delegate` |
| `GROK_DELEGATE_POOL` | `$GROK_DELEGATE_STATE/pool.json` |
| `GROK_DELEGATES_DIR` | `$GROK_DELEGATE_STATE/delegates` |
| `GROK_BUS_DIR` | `$GROK_DELEGATE_STATE/bus` |
| `GROK_BIN` | `grok` on `PATH`, then `~/.grok/bin/grok` |
| `GROK_DELEGATE_MAX_TURNS` | `200` |
| `GROK_DELEGATE_TIMEOUT_SEC` | `3300` |

Do not commit the state directory. Job and result files contain task text.

## Trust

The bridge listens on localhost with no token. Anyone who can connect to that
port on this machine can start a worker.

Each worker is started with `--permission-mode bypassPermissions`. It can use
Grok tools in the directory you pass as `cwd`. Delegate only work you would
trust a local agent to do.

The `grok` command this bridge expects:

```text
grok [--resume SESSION]
  --prompt-file FILE
  --output-format json
  --permission-mode bypassPermissions
  --cwd DIR
  --no-alt-screen
  --max-turns N
  --no-auto-update
```

A successful JSON result has `text` and `sessionId`. The next task in the same
directory resumes that session.

## Checks

```bash
python3 -m unittest tests.test_bridge
```

The tests use a temporary state directory. They do not start `grok`.

## Layout

```text
mcp_server.py    MCP stdio and localhost HTTP
run.py           start a grok child and record the job
children.py      SIGCHLD reaper for those children
pool.py          context index
bus.py           file pub/sub
claim_slot.py    print the pool; it does not register a terminal
```

A numeric name such as `8` still maps to the historical pin name
`docpublishing-delegate8`. New contexts are named `w1`, `w2`, and so on.
