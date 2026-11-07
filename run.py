#!/usr/bin/env python3
"""Run one delegated task on the local Grok subscription.

Stdin is the task. Prints a single JSON object to stdout:
{"ok": true, "text": "...", "sessionId": "...", "resultPath": "..."}
or {"ok": false, "error": "...", "resultPath": "..."}.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

HOME = Path.home()
STATE_DIR = Path(os.environ.get("GROK_DELEGATE_STATE", HOME / ".codex" / "grok-delegate"))
RESULT_DIR = STATE_DIR / "results"
JOBS_DIR = STATE_DIR / "jobs"
DELEGATES_DIR = Path(os.environ.get("GROK_DELEGATES_DIR", STATE_DIR / "delegates"))
# Numeric agent ids still resolve to this historical pin name.
POOL_NAME = re.compile(r"^docpublishing-delegate([1-9]|10)$")
GROK = os.environ.get("GROK_BIN") or shutil.which("grok") or str(HOME / ".grok" / "bin" / "grok")
# Always passed to the headless grok Codex starts. Omitting it leaves Grok on an
# implicit stop and produces error_max_turns with an unfinished session.
# 200 clears the old 30-turn ceiling. Set GROK_DELEGATE_MAX_TURNS to override.
MAX_TURNS = int(os.environ.get("GROK_DELEGATE_MAX_TURNS", "200"))
TIMEOUT_SEC = int(os.environ.get("GROK_DELEGATE_TIMEOUT_SEC", "3300"))


def _covers(root: Path, work: Path) -> bool:
    try:
        work.resolve().relative_to(root.expanduser().resolve())
    except ValueError:
        return False
    return True


def normalize_agent(agent: str | None) -> str | None:
    text = (agent or "").strip()
    if not text:
        return None
    if text.isdigit():
        text = f"docpublishing-delegate{int(text)}"
    if not POOL_NAME.match(text):
        return None
    return text


def pool_pins(work: Path) -> list[dict]:
    if not DELEGATES_DIR.is_dir():
        return []
    found = []
    for path in DELEGATES_DIR.glob("docpublishing-delegate*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        name = str(data.get("name") or path.stem)
        match = POOL_NAME.match(name)
        session_id = str(data.get("sessionId") or "").strip()
        root = Path(str(data.get("cwd") or ""))
        if not match or not session_id or not root.is_dir() or not _covers(root, work):
            continue
        found.append(
            {"slot": int(match.group(1)), "name": name, "sessionId": session_id}
        )
    return sorted(found, key=lambda item: item["slot"])


def pinned_session(work: Path, agent: str | None = None) -> tuple[str | None, str | None, str | None]:
    """Legacy pin lookup. New workers come from pool.acquire, which also checks the pid."""
    requested = normalize_agent(agent)
    if agent and not requested:
        return None, None, "agent must be docpublishing-delegate1 through docpublishing-delegate10"
    pins = pool_pins(work)
    if requested:
        for pin in pins:
            if pin["name"] == requested:
                return pin["sessionId"], pin["name"], None
        return None, requested, f"{requested} is not registered"
    if pins:
        state_path = DELEGATES_DIR / "docpublishing-delegate.rr"
        last = 0
        try:
            last = int(state_path.read_text().strip() or "0")
        except (OSError, ValueError):
            last = 0
        chosen = next((pin for pin in pins if pin["slot"] > last), pins[0])
        state_path.write_text(str(chosen["slot"]) + "\n")
        return chosen["sessionId"], chosen["name"], None
    if not DELEGATES_DIR.is_dir():
        return None, None, None
    resolved = work.resolve()
    best_id = None
    best_len = -1
    for path in DELEGATES_DIR.glob("*.json"):
        if POOL_NAME.match(path.stem):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        session_id = str(data.get("sessionId") or "").strip()
        root = Path(str(data.get("cwd") or "")).expanduser()
        if not session_id or not root.is_dir() or not _covers(root, resolved):
            continue
        length = len(str(root.resolve()))
        if length > best_len:
            best_id = session_id
            best_len = length
    return best_id, None, None

def _preamble() -> str:
    bus_cli = Path(__file__).resolve().parent / "bus.py"
    return f"""You are the worker for a Codex coordinator. This task runs on the local Grok CLI.

Do the task in the working directory. Use tools. Do not call the codex CLI and do not start a Codex sub-agent.

When you finish, reply with:
- what you changed or found
- what you verified
- any blocker

You and the coordinator each have a pub queue and a sub queue. Talk on that bus
while you work. Do not wait to be the only running worker.

Your bus name is written at the end of this prompt. Send a message to the coordinator:
  python3 {bus_cli} pub --as <bus name> --to coord --kind message --text "..."

Read messages the coordinator sent you:
  python3 {bus_cli} sub --as <bus name>

Use kind "waiting" when you need a decision, then read your sub queue before continuing. Use kind "result" for the final reply.

TASK:
"""


def _prepare(task: str, cwd: str, agent: str | None):
    task = (task or "").strip()
    if not task:
        return None, {"ok": False, "error": "empty task"}
    work = Path(cwd).expanduser()
    if not work.is_dir():
        return None, {"ok": False, "error": f"cwd is not a directory: {work}"}
    if not Path(GROK).is_file():
        return None, {"ok": False, "error": f"grok binary not found at {GROK}"}
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    prompt_path = None
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", prefix="grok-delegate-", delete=False
    ) as handle:
        handle.write(_preamble())
        handle.write(task)
        handle.write("\n")
        prompt_path = handle.name
    from pool import acquire

    worker, pin_error = acquire(agent, work)
    if pin_error:
        os.unlink(prompt_path)
        return None, {"ok": False, "error": pin_error, "agent": (worker or {}).get("id"), "pid": (worker or {}).get("pid")}
    session_id = worker.get("sessionId") or None
    agent_name = worker.get("id")
    with open(prompt_path, "a", encoding="utf-8") as handle:
        handle.write(
            f"\nYour bus name is {agent_name}. Your context is {worker.get('context')}. "
            "Use that bus name with bus.py --as.\n"
        )
    cmd = [GROK]
    if session_id:
        cmd.extend(["--resume", session_id])
    cmd.extend(
        [
            "--prompt-file",
            prompt_path,
            "--output-format",
            "json",
            "--permission-mode",
            "bypassPermissions",
            "--cwd",
            str(work),
            "--no-alt-screen",
            "--max-turns",
            str(MAX_TURNS),
            "--no-auto-update",
        ]
    )
    stamp = time.strftime("%Y%m%dT%H%M%S")
    result_path = RESULT_DIR / f"{stamp}-{os.getpid()}.json"
    return {
        "cmd": cmd,
        "work": work,
        "prompt_path": prompt_path,
        "session_id": session_id,
        "agent_name": agent_name,
        "reservation": worker.get("job"),
        "result_path": result_path,
    }, None


def _interpret(stdout: str, stderr: str, returncode: int, agent_name, result_path: Path) -> dict:
    raw = (stdout or "").strip()
    parsed = None
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(raw[start : end + 1])
                except json.JSONDecodeError:
                    parsed = None
    if returncode != 0 and not isinstance(parsed, dict):
        payload = {
            "ok": False,
            "error": f"grok exited {returncode}",
            "stdout": raw[-4000:],
            "stderr": (stderr or "")[-4000:],
            "agent": agent_name,
        }
    else:
        text = ""
        session_id = None
        if isinstance(parsed, dict):
            text = parsed.get("text") or ""
            session_id = parsed.get("sessionId")
            if not text and parsed.get("error"):
                payload = {
                    "ok": False,
                    "error": str(parsed.get("error")),
                    "sessionId": session_id,
                    "agent": agent_name,
                    "stderr": (stderr or "")[-2000:],
                }
                result_path.write_text(json.dumps(payload, indent=2))
                payload["resultPath"] = str(result_path)
                return payload
        if not text:
            text = raw
        payload = {
            "ok": True,
            "text": text.strip(),
            "sessionId": session_id,
            "agent": agent_name,
            "returncode": returncode,
        }
    result_path.write_text(json.dumps(payload, indent=2))
    payload["resultPath"] = str(result_path)
    return payload


def _write_job(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


def run_task(task: str, cwd: str, agent: str | None = None) -> dict:
    """Start a managed worker and wait for its job record.

    The wait polls the job file. It does not hold the MCP read loop, so another
    delegate call can proceed while this one is still running.
    """
    started = start_task(task, cwd, agent)
    if started.get("status") != "running":
        return started
    deadline = time.time() + TIMEOUT_SEC + 30
    while time.time() < deadline:
        state = job_status(started["job"])
        if state.get("status") not in ("running", "exiting"):
            return state
        time.sleep(0.25)
    return {
        "ok": False,
        "error": f"job {started['job']} did not finish",
        "agent": started.get("agent"),
        "job": started["job"],
    }


def _drain(pipe, sink: list) -> None:
    if pipe is None:
        sink.append("")
        return
    try:
        sink.append(pipe.read())
    except Exception:
        sink.append("")
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _capture(proc: subprocess.Popen, timeout: float):
    """Read the child's pipes. The kernel's SIGCHLD, not this read, is what reaps it."""
    from children import supervisor

    stdout, stderr = [], []
    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, stdout), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    code = supervisor.wait(proc.pid, timeout)
    timed_out = code is None
    if timed_out:
        supervisor.stop(proc)
        code = supervisor.wait(proc.pid, 5)
        if code is None:
            supervisor.kill(proc)
            code = supervisor.wait(proc.pid, 5)
    for thread in threads:
        thread.join(timeout=5)
    return (
        stdout[0] if stdout else "",
        stderr[0] if stderr else "",
        -1 if code is None else code,
        timed_out,
    )


def _watch_job(proc: subprocess.Popen, prepared: dict, job_path: Path, job: dict) -> None:
    from children import supervisor

    payload = {
        "ok": False,
        "error": "worker ended before its result was recorded",
        "agent": prepared.get("agent_name"),
    }
    try:
        stdout, stderr, returncode, timed_out = _capture(proc, TIMEOUT_SEC)
        if timed_out:
            payload = {
                "ok": False,
                "error": f"grok timed out after {TIMEOUT_SEC}s",
                "agent": prepared["agent_name"],
                "stdout": (stdout or "")[-4000:] if isinstance(stdout, str) else "",
                "stderr": (stderr or "")[-4000:] if isinstance(stderr, str) else "",
            }
            prepared["result_path"].write_text(json.dumps(payload, indent=2))
            payload["resultPath"] = str(prepared["result_path"])
        else:
            payload = _interpret(stdout, stderr, returncode, prepared["agent_name"], prepared["result_path"])
    except Exception as exc:  # noqa: BLE001 — the context must be released if the reader fails
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "agent": prepared.get("agent_name"),
        }
    finally:
        try:
            os.unlink(prepared["prompt_path"])
        except OSError:
            pass
    job = dict(job)
    job["status"] = "done" if payload.get("ok") else "error"
    job["ok"] = bool(payload.get("ok"))
    job["resultPath"] = payload.get("resultPath")
    job["error"] = payload.get("error")
    job["sessionId"] = payload.get("sessionId") or job.get("sessionId")
    job["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _write_job(job_path, job)
    agent_name = prepared.get("agent_name") or ""
    if agent_name:
        from pool import mark_idle
        mark_idle(agent_name, payload.get("sessionId"), pid=proc.pid, job=job.get("id"))
    supervisor.forget(proc.pid)
    try:
        import bus
        kind = "result" if payload.get("ok") else "error"
        body = payload.get("text") or payload.get("error") or ""
        bus.publish(agent_name, "coord", body, kind=kind, job=job.get("id"))
        bus.set_stream(agent_name, status="done" if payload.get("ok") else "error", job=job.get("id"))
    except Exception:
        pass


def start_task(task: str, cwd: str, agent: str | None = None) -> dict:
    """Start a headless Grok worker and return without waiting for it."""
    prepared, error = _prepare(task, cwd, agent)
    if error:
        return error
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    job_path = JOBS_DIR / f"{job_id}.json"
    from children import supervisor

    try:
        proc = subprocess.Popen(
            prepared["cmd"],
            cwd=str(prepared["work"]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        from pool import release
        release(prepared["agent_name"], prepared.get("reservation"), prepared.get("session_id"))
        try:
            os.unlink(prepared["prompt_path"])
        except OSError:
            pass
        return {"ok": False, "error": str(exc), "agent": prepared["agent_name"]}
    supervisor.track(proc)
    from pool import mark_running
    if not mark_running(prepared["agent_name"], proc.pid, job_id, prepared.get("reservation")):
        supervisor.stop(proc)
        supervisor.wait(proc.pid, 5)
        supervisor.forget(proc.pid)
        try:
            os.unlink(prepared["prompt_path"])
        except OSError:
            pass
        return {
            "ok": False,
            "error": f"{prepared['agent_name']} changed before its pid could be recorded",
            "agent": prepared["agent_name"],
        }
    job = {
        "id": job_id,
        "status": "running",
        "pid": proc.pid,
        "agent": prepared["agent_name"],
        "sessionId": prepared["session_id"],
        "cwd": str(prepared["work"]),
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _write_job(job_path, job)
    agent_name = prepared["agent_name"] or "unpinned"
    try:
        import bus
        bus.publish("coord", agent_name, task.strip()[:4000], kind="task", job=job_id)
        bus.set_stream(agent_name, status="running", job=job_id, last_from="coord", last_text=task.strip(), last_id="")
    except Exception:
        pass
    threading.Thread(
        target=_watch_job, args=(proc, prepared, job_path, job), daemon=True
    ).start()
    return {"ok": True, "status": "running", "job": job_id, "pid": proc.pid, "agent": agent_name}


def job_status(job_id: str) -> dict:
    job_id = (job_id or "").strip()
    path = JOBS_DIR / f"{job_id}.json"
    if not path.is_file():
        return {"ok": False, "error": f"unknown job {job_id}"}
    job = json.loads(path.read_text())
    if job.get("status") == "running":
        from pool import alive
        pid = job.get("pid")
        if alive(pid):
            return {"ok": True, "status": "running", "job": job_id, "agent": job.get("agent"), "pid": pid}
        return {"ok": True, "status": "exiting", "job": job_id, "agent": job.get("agent"), "pid": pid}
    result_path = job.get("resultPath")
    text = ""
    if result_path and Path(result_path).is_file():
        try:
            saved = json.loads(Path(result_path).read_text())
            text = saved.get("text") or ""
            job["ok"] = saved.get("ok", job.get("ok"))
            job["error"] = saved.get("error") or job.get("error")
            job["sessionId"] = saved.get("sessionId") or job.get("sessionId")
        except (OSError, json.JSONDecodeError):
            text = ""
    return {
        "ok": bool(job.get("ok")),
        "status": job.get("status") or "done",
        "job": job_id,
        "agent": job.get("agent"),
        "sessionId": job.get("sessionId"),
        "error": job.get("error"),
        "resultPath": result_path,
        "text": text,
    }


def main() -> int:
    from children import supervisor
    supervisor.install()
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--agent", default="")
    args = parser.parse_args()
    task = sys.stdin.read()
    result = run_task(task, args.cwd, args.agent or None)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — last-resort envelope for the hook
        json.dump({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sys.stdout)
        sys.stdout.write("\n")
        raise SystemExit(1)
