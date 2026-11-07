#!/usr/bin/env python3
"""Index of Grok contexts the MCP process owns.

There is no fixed slot count. A context is the resolved working directory.
Its row holds the session to resume and the pid of the grok child this server
started. The row is busy only while that pid is alive, or while this process
still owns the exit the kernel just reported.
"""

import json
import os
import time
import uuid
from pathlib import Path

HOME = Path.home()
STATE_DIR = Path(os.environ.get("GROK_DELEGATE_STATE", HOME / ".codex" / "grok-delegate"))
POOL_PATH = Path(os.environ.get("GROK_DELEGATE_POOL", STATE_DIR / "pool.json"))
PINS_DIR = Path(os.environ.get("GROK_DELEGATES_DIR", STATE_DIR / "delegates"))
RESERVED_STALE_SEC = 30
EXITING_STALE_SEC = 60


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stamp(value: str) -> float:
    try:
        return time.mktime(time.strptime((value or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0


def _empty() -> dict:
    return {"seq": 0, "imported": False, "contexts": []}


def _load() -> dict:
    try:
        data = json.loads(POOL_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        data = _empty()
    if "contexts" not in data and isinstance(data.get("workers"), list):
        data["contexts"] = data.pop("workers")
    data.setdefault("seq", 0)
    data.setdefault("imported", False)
    data.setdefault("contexts", [])
    return data


def _save(data: dict) -> None:
    POOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = POOL_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(POOL_PATH)


def _context_key(work: Path) -> str:
    return str(work.expanduser().resolve())


def _by_id(data: dict, name: str):
    for worker in data["contexts"]:
        if worker.get("id") == name:
            return worker
    return None


def _by_context(data: dict, context: str):
    for worker in data["contexts"]:
        if worker.get("context") == context:
            return worker
    return None


def _next_id(data: dict) -> str:
    used = {worker.get("id") for worker in data["contexts"]}
    number = int(data.get("seq") or 0) + 1
    while f"w{number}" in used:
        number += 1
    data["seq"] = number
    return f"w{number}"


def _add(data: dict, context: str) -> dict:
    worker = {
        "id": _next_id(data),
        "context": context,
        "sessionId": None,
        "pid": None,
        "status": "idle",
        "job": None,
        "updatedAt": _now(),
    }
    data["contexts"].append(worker)
    return worker


def _import_pins(data: dict) -> None:
    """One idle row per distinct pin directory. A pin is not a live binding."""
    if not PINS_DIR.is_dir():
        return
    known = {worker.get("context") for worker in data["contexts"]}
    newest = {}
    for path in sorted(PINS_DIR.glob("*.json")):
        try:
            pin = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        raw = str(pin.get("cwd") or "")
        if not raw:
            continue
        try:
            context = str(Path(raw).expanduser().resolve())
        except OSError:
            continue
        mtime = path.stat().st_mtime
        previous = newest.get(context)
        if previous is None or mtime >= previous[0]:
            newest[context] = (mtime, pin, path.stem)
    for context, (_, pin, stem) in newest.items():
        if context in known:
            continue
        data["contexts"].append(
            {
                "id": stem,
                "context": context,
                "sessionId": str(pin.get("sessionId") or "") or None,
                "pid": None,
                "status": "idle",
                "job": None,
                "updatedAt": _now(),
            }
        )
        known.add(context)


def _owns(pid) -> bool:
    try:
        from children import supervisor
    except Exception:
        return False
    return supervisor.owns(pid)


def _reconcile(data: dict) -> None:
    now = time.time()
    for worker in data["contexts"]:
        status = worker.get("status")
        pid = worker.get("pid")
        if status == "running" and not alive(pid):
            if _owns(pid):
                worker["status"] = "exiting"
                worker["updatedAt"] = _now()
            else:
                worker["status"] = "idle"
                worker["pid"] = None
                worker["job"] = None
                worker["updatedAt"] = _now()
        elif status == "exiting" and now - _stamp(worker.get("updatedAt") or "") > EXITING_STALE_SEC:
            worker["status"] = "idle"
            worker["pid"] = None
            worker["job"] = None
            worker["updatedAt"] = _now()
        elif status == "reserved" and now - _stamp(worker.get("updatedAt") or "") > RESERVED_STALE_SEC:
            worker["status"] = "idle"
            worker["pid"] = None
            worker["job"] = None
            worker["updatedAt"] = _now()


def _held(worker: dict) -> bool:
    return worker.get("status") in ("running", "exiting", "reserved")


def _busy(worker: dict) -> str:
    pid = worker.get("pid")
    context = worker.get("context")
    name = worker.get("id")
    if worker.get("status") == "running":
        return f"{name} context {context} is running as pid {pid}"
    if worker.get("status") == "exiting":
        return f"{name} context {context} pid {pid} has exited and its result is still being recorded"
    return f"{name} context {context} is reserved"


def _view(worker: dict) -> dict:
    shown = dict(worker)
    shown["alive"] = worker.get("status") == "running" and alive(worker.get("pid"))
    return shown


def _ensure(data: dict) -> None:
    if data.get("imported"):
        return
    _import_pins(data)
    data["imported"] = True


def _snapshot(data: dict) -> list[dict]:
    _ensure(data)
    _reconcile(data)
    return [_view(worker) for worker in sorted(data["contexts"], key=lambda item: item.get("id") or "")]


def snapshot() -> list[dict]:
    return _locked(_snapshot)


def _reserve(worker: dict) -> None:
    worker["status"] = "reserved"
    worker["pid"] = None
    worker["job"] = "reserve-" + uuid.uuid4().hex
    worker["updatedAt"] = _now()


def _acquire(data: dict, agent: str | None, work: Path):
    _ensure(data)
    _reconcile(data)
    context = _context_key(work)
    requested = (agent or "").strip()
    alias = f"w{int(requested)}" if requested.isdigit() else requested
    named = _by_id(data, requested) if requested else None
    if named is None and alias != requested:
        named = _by_id(data, alias)
    if named is not None and named.get("context") != context and _held(named):
        return dict(named), (
            f"{named.get('id')} is in use for {named.get('context')} "
            f"(pid {named.get('pid')}). Omit agent to run {context} as its own context."
        )
    worker = _by_context(data, context)
    if worker is None:
        worker = _add(data, context)
    if _held(worker):
        return dict(worker), _busy(worker)
    _reserve(worker)
    return dict(worker), None


def acquire(agent: str | None, work: Path):
    def mutate(data: dict):
        return _acquire(data, agent, work)

    return _locked(mutate)


def mark_running(name: str, pid: int, job: str, reservation: str | None = None) -> bool:
    def mutate(data: dict):
        worker = _by_id(data, name)
        if worker is None:
            return False
        if reservation is not None and worker.get("job") != reservation:
            return False
        current = worker.get("pid")
        if worker.get("status") == "running" and alive(current) and current != pid:
            return False
        worker["status"] = "running"
        worker["pid"] = int(pid)
        worker["job"] = job
        worker["updatedAt"] = _now()
        return True

    return bool(_locked(mutate))


def mark_idle(name: str, session_id: str | None = None, pid: int | None = None, job: str | None = None):
    def mutate(data: dict):
        worker = _by_id(data, name)
        if worker is None:
            return None
        if pid is not None and worker.get("pid") not in (None, pid):
            return None
        if job is not None and worker.get("job") not in (None, job):
            return None
        if session_id:
            worker["sessionId"] = session_id
        worker["status"] = "idle"
        worker["pid"] = None
        worker["job"] = None
        worker["updatedAt"] = _now()
        return worker

    return _locked(mutate)


def release(name: str, reservation: str | None, session_id: str | None = None) -> bool:
    def mutate(data: dict):
        worker = _by_id(data, name)
        if worker is None or not reservation or worker.get("job") != reservation:
            return False
        if session_id:
            worker["sessionId"] = session_id
        worker["status"] = "idle"
        worker["pid"] = None
        worker["job"] = None
        worker["updatedAt"] = _now()
        return True

    return bool(_locked(mutate))


def _locked(fn):
    import fcntl

    POOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = POOL_PATH.with_suffix(".lock")
    with open(lock_path, "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        data = _load()
        result = fn(data)
        _save(data)
        return result
