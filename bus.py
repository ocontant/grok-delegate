#!/usr/bin/env python3
"""Local pub/sub queues for the Codex coordinator and each Grok slot.

Every party has two directories:
  pub/  messages that party sent
  sub/  messages addressed to that party

A publish writes the same message into the sender's pub queue and the
recipient's sub queue. Neither side holds a lock across the other.
"""

import argparse
import json
import os
import time
import uuid
from pathlib import Path

HOME = Path.home()
STATE_DIR = Path(os.environ.get("GROK_DELEGATE_STATE", HOME / ".codex" / "grok-delegate"))
BUS_DIR = Path(os.environ.get("GROK_BUS_DIR", STATE_DIR / "bus"))
COORD = "coord"


def party_name(name: str | None) -> str:
    text = (name or "").strip()
    if not text or text in ("coord", "coordinator"):
        return COORD
    # Legacy pin files used this name for slots 1 through 10.
    if text.isdigit():
        return f"docpublishing-delegate{int(text)}"
    return text


def _party_dir(name: str) -> Path:
    path = BUS_DIR / "parties" / party_name(name)
    (path / "pub").mkdir(parents=True, exist_ok=True)
    (path / "sub").mkdir(parents=True, exist_ok=True)
    return path


def _message(sender: str, recipient: str, text: str, kind: str, job: str | None) -> dict:
    return {
        "id": f"{time.time_ns()}-{uuid.uuid4().hex[:8]}",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "from": party_name(sender),
        "to": party_name(recipient),
        "kind": kind or "message",
        "job": job or "",
        "text": text or "",
    }


def _write(directory: Path, message: dict) -> None:
    path = directory / f"{message['id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(message, indent=2) + "\n")
    tmp.replace(path)


def publish(sender: str, recipient: str, text: str, kind: str = "message", job: str | None = None) -> dict:
    message = _message(sender, recipient, text, kind, job)
    sender_name = message["from"]
    recipient_name = message["to"]
    _write(_party_dir(sender_name) / "pub", message)
    _write(_party_dir(recipient_name) / "sub", message)
    agent = sender_name if sender_name != COORD else recipient_name
    if agent != COORD:
        status = kind if kind in ("running", "waiting", "done", "error", "idle") else None
        set_stream(
            agent,
            status=status,
            job=job,
            last_from=sender_name,
            last_text=text,
            last_id=message["id"],
        )
    return message


def _cursor_path(name: str) -> Path:
    return _party_dir(name) / "cursor"


def receive(name: str, limit: int = 20, consume: bool = True) -> list[dict]:
    """Return the next inbox messages in order. consume advances this party's cursor."""
    inbox = _party_dir(name) / "sub"
    cursor = ""
    cursor_file = _cursor_path(name)
    if cursor_file.is_file():
        cursor = cursor_file.read_text().strip()
    found = []
    last = cursor
    for path in sorted(inbox.glob("*.json")):
        message_id = path.stem
        if cursor and message_id <= cursor:
            continue
        try:
            message = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        found.append(message)
        last = message_id
        if len(found) >= limit:
            break
    if consume and last and last != cursor:
        cursor_file.write_text(last + "\n")
    return found


def set_stream(
    name: str,
    status: str | None = None,
    job: str | None = None,
    last_from: str | None = None,
    last_text: str | None = None,
    last_id: str | None = None,
) -> dict:
    streams = BUS_DIR / "streams"
    streams.mkdir(parents=True, exist_ok=True)
    path = streams / f"{party_name(name)}.json"
    current = {}
    if path.is_file():
        try:
            current = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            current = {}
    current["name"] = party_name(name)
    if status:
        current["status"] = status
    if job is not None:
        current["job"] = job
    if last_from:
        current["lastFrom"] = last_from
    if last_text is not None:
        current["lastText"] = (last_text or "")[:500]
    if last_id:
        current["lastId"] = last_id
    current["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2) + "\n")
    tmp.replace(path)
    return current


def streams() -> list[dict]:
    directory = BUS_DIR / "streams"
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob("*.json")):
        try:
            found.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    pub = sub.add_parser("pub")
    pub.add_argument("--as", dest="sender", required=True)
    pub.add_argument("--to", dest="recipient", required=True)
    pub.add_argument("--text", required=True)
    pub.add_argument("--kind", default="message")
    pub.add_argument("--job", default="")

    receive_cmd = sub.add_parser("sub")
    receive_cmd.add_argument("--as", dest="name", required=True)
    receive_cmd.add_argument("--limit", type=int, default=20)
    receive_cmd.add_argument("--peek", action="store_true")

    sub.add_parser("streams")
    args = parser.parse_args()
    if args.cmd == "pub":
        print(json.dumps(publish(args.sender, args.recipient, args.text, args.kind, args.job or None)))
        return 0
    if args.cmd == "sub":
        print(json.dumps(receive(args.name, args.limit, consume=not args.peek)))
        return 0
    print(json.dumps(streams()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
