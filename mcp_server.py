#!/usr/bin/env python3
"""MCP server: one process tracks the Grok children it starts.

`--serve` listens on localhost for streamable HTTP so every client shares
that process. With no arguments it speaks stdio, one client per process.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bus  # noqa: E402
from pool import snapshot  # noqa: E402
from run import job_status, run_task, start_task  # noqa: E402

PROTOCOL = "2024-11-05"
DEFAULT_HTTP = "127.0.0.1:7391"
STATE_DIR = Path(os.environ.get("GROK_DELEGATE_STATE", Path.home() / ".codex" / "grok-delegate"))
PID_PATH = STATE_DIR / "serve.pid"
WRITE = threading.Lock()
_SINK = threading.local()


def send(message: dict) -> None:
    box = getattr(_SINK, "box", None)
    if box is not None:
        box.append(message)
        return
    line = json.dumps(message) + "\n"
    with WRITE:
        sys.stdout.write(line)
        sys.stdout.flush()


def _invoke(message: dict) -> dict | None:
    box: list[dict] = []
    _SINK.box = box
    try:
        _guarded(message)
    finally:
        _SINK.box = None
    return box[-1] if box else None


def tool_result(text: str, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def finished_text(result: dict) -> tuple[str, bool]:
    if result.get("status") in ("running", "exiting"):
        state = "still running" if result.get("status") == "running" else "exited and its result is being recorded"
        return (
            f"Grok worker is {state}.\n"
            f"job: {result.get('job')}\n"
            f"context: {result.get('agent') or 'unknown'}\n"
            f"pid: {result.get('pid')}\n"
            "Call delegate_wait with this job id for the final reply.",
            False,
        )
    if not result.get("ok"):
        return (
            "Grok worker failed. Do not retry this task with spawn_agent or codex exec.\n"
            f"agent: {result.get('agent') or 'unpinned'}\n"
            f"{result.get('error')}\n"
            f"Details: {result.get('resultPath', '')}",
            True,
        )
    return (
        "Grok worker finished. This result is from the Grok subscription, not Codex.\n"
        f"agent: {result.get('agent') or 'unpinned'}\n"
        f"session: {result.get('sessionId') or 'unknown'}\n"
        f"saved: {result.get('resultPath')}\n\n"
        f"{result.get('text') or ''}",
        False,
    )


def handle(message: dict) -> None:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if method in ("notifications/initialized", "initialized"):
        return

    if msg_id is None:
        return

    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "grok-delegate", "version": "1.0.0"},
                },
            }
        )
        return

    if method == "tools/list":
        send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": [
                        {
                            "name": "delegate",
                            "description": (
                                "Start a Grok worker as a child of this server. The server indexes "
                                "that child by context, which is the cwd, and by its pid. The kernel "
                                "SIGCHLD tells this server when the child exits. A context is busy "
                                "only while its pid is alive. A different cwd is another context and "
                                "can run at the same time. There is no fixed slot count. Omit agent. "
                                "wait false returns a job id immediately. wait true waits for that "
                                "job without blocking the next MCP call."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "task": {
                                        "type": "string",
                                        "description": "Self-contained task for the Grok worker.",
                                    },
                                    "cwd": {
                                        "type": "string",
                                        "description": "Absolute workspace directory for the worker.",
                                    },
                                    "agent": {
                                        "type": "string",
                                        "description": (
                                            "Optional context id returned by pool, such as w3. "
                                            "Omit it. The cwd is the context. A different directory "
                                            "runs as its own child."
                                        ),
                                    },
                                    "wait": {
                                        "type": "boolean",
                                        "description": (
                                            "False starts the worker and returns a job id immediately. "
                                            "True waits for the worker. Use false for every member "
                                            "of a parallel batch."
                                        ),
                                    },
                                },
                                "required": ["task", "cwd"],
                                "additionalProperties": False,
                            },
                        },
                        {
                            "name": "bus_publish",
                            "description": (
                                "Publish a message from the coordinator onto one agent's sub queue. "
                                "The same message is stored on the coordinator's pub queue. "
                                "Does not wait for the agent."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "to": {
                                        "type": "string",
                                        "description": "Context id shown by pool, such as w3.",
                                    },
                                    "text": {"type": "string"},
                                    "kind": {
                                        "type": "string",
                                        "description": "message, task, waiting, result, error, running, or done.",
                                    },
                                    "job": {"type": "string"},
                                },
                                "required": ["to", "text"],
                                "additionalProperties": False,
                            },
                        },
                        {
                            "name": "bus_receive",
                            "description": (
                                "Read the next messages on the coordinator sub queue. "
                                "Each agent's pub lands here. Returns immediately. "
                                "An empty list means nothing new is waiting."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "limit": {"type": "integer"},
                                    "peek": {
                                        "type": "boolean",
                                        "description": "True leaves the messages on the queue.",
                                    },
                                },
                                "additionalProperties": False,
                            },
                        },
                        {
                            "name": "bus_streams",
                            "description": (
                                "Current state of every agent stream: status, job, and the last "
                                "message. Use this to see slot 8 and slot 9 without waiting on either."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        },
                        {
                            "name": "pool",
                            "description": (
                                "Index of contexts and the grok pids this server started. "
                                "alive is true only while that child is still running. "
                                "There is no fixed slot count. Terminals do not register here."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        },
                        {
                            "name": "delegate_wait",
                            "description": (
                                "Read one background delegate job. Returns running, or the worker "
                                "result when the job has finished. Does not start a worker."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "job": {
                                        "type": "string",
                                        "description": "Job id returned by delegate with wait false.",
                                    }
                                },
                                "required": ["job"],
                                "additionalProperties": False,
                            },
                        }
                    ]
                },
            }
        )
        return

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "bus_publish":
            message = bus.publish(
                "coord",
                str(arguments.get("to") or ""),
                str(arguments.get("text") or ""),
                kind=str(arguments.get("kind") or "message"),
                job=str(arguments.get("job") or "") or None,
            )
            send({"jsonrpc": "2.0", "id": msg_id, "result": tool_result(json.dumps(message), False)})
            return
        if name == "bus_receive":
            limit = int(arguments.get("limit") or 20)
            messages = bus.receive("coord", limit=limit, consume=arguments.get("peek") is not True)
            body = json.dumps(messages, indent=2) if messages else "[]"
            send({"jsonrpc": "2.0", "id": msg_id, "result": tool_result(body, False)})
            return
        if name == "bus_streams":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": tool_result(json.dumps(bus.streams(), indent=2), False),
                }
            )
            return
        if name == "pool":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": tool_result(json.dumps(snapshot(), indent=2), False),
                }
            )
            return
        if name == "delegate_wait":
            result = job_status(str(arguments.get("job") or ""))
            text, is_error = finished_text(result)
            send({"jsonrpc": "2.0", "id": msg_id, "result": tool_result(text, is_error)})
            return
        if name != "delegate":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": tool_result(f"unknown tool {name}", True),
                }
            )
            return
        agent = str(arguments.get("agent") or "") or None
        if arguments.get("wait") is False:
            result = start_task(str(arguments.get("task") or ""), str(arguments.get("cwd") or ""), agent)
        else:
            result = run_task(str(arguments.get("task") or ""), str(arguments.get("cwd") or ""), agent)
        text, is_error = finished_text(result)
        send({"jsonrpc": "2.0", "id": msg_id, "result": tool_result(text, is_error)})
        return

    if method == "ping":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        return

    send(
        {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    )


def _already_serving() -> int | None:
    try:
        pid = int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None
    command = Path(f"/proc/{pid}/cmdline")
    try:
        raw = command.read_bytes().replace(b"\x00", b" ").decode()
    except OSError:
        return None
    if "mcp_server.py" in raw and "--serve" in raw:
        return pid
    return None


def _origin_allowed(origin: str, host: str) -> bool:
    if not origin:
        return True
    allowed = {f"http://{host}", f"http://127.0.0.1:{host.rsplit(':', 1)[-1]}", f"http://localhost:{host.rsplit(':', 1)[-1]}"}
    return origin.rstrip("/") in allowed


class _MCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _reject(self, code: int) -> None:
        body = json.dumps({"jsonrpc": "2.0", "error": {"code": code, "message": self.responses.get(code, "")}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._reject(405)

    def do_DELETE(self) -> None:
        self._reject(405)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/mcp":
            self._reject(404)
            return
        host = self.headers.get("Host") or ""
        if not host.startswith("127.0.0.1:") and not host.startswith("localhost:"):
            self._reject(403)
            return
        if not _origin_allowed(self.headers.get("Origin") or "", host):
            self._reject(403)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 8_000_000:
            self._reject(400)
            return
        try:
            message = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._reject(400)
            return
        if not isinstance(message, dict):
            self._reject(400)
            return
        accept = self.headers.get("Accept") or "application/json"
        reply = _invoke(message)
        if message.get("id") is None or reply is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = json.dumps(reply)
        version = self.headers.get("MCP-Protocol-Version") or PROTOCOL
        if "application/json" not in accept and "text/event-stream" in accept:
            body = f"event: message\ndata: {payload}\n\n".encode()
            content_type = "text/event-stream"
        else:
            body = payload.encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("MCP-Protocol-Version", version)
        self.end_headers()
        self.wfile.write(body)


def serve(address: str) -> int:
    from children import supervisor

    existing = _already_serving()
    if existing is not None:
        print(f"grok_delegate already serving as pid {existing}", file=sys.stderr)
        return 0
    host, _, port_text = address.rpartition(":")
    host = host or "127.0.0.1"
    if host not in ("127.0.0.1", "localhost"):
        print("refusing to listen outside localhost", file=sys.stderr)
        return 1
    supervisor.install()
    server = ThreadingHTTPServer((host, int(port_text)), _MCPHandler)
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()) + "\n")
    print(f"grok_delegate listening on http://{host}:{port_text}/mcp", file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        try:
            if PID_PATH.read_text().strip() == str(os.getpid()):
                PID_PATH.unlink()
        except OSError:
            pass
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        return serve(sys.argv[2] if len(sys.argv) > 2 else DEFAULT_HTTP)
    from children import supervisor
    supervisor.install()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        threading.Thread(target=_guarded, args=(message,), daemon=True).start()
    return 0


def _guarded(message: dict) -> None:
    try:
        handle(message)
    except Exception as exc:  # noqa: BLE001 — keep the stdio session alive
        msg_id = message.get("id")
        if msg_id is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"},
                }
            )


if __name__ == "__main__":
    raise SystemExit(main())
