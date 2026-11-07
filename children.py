#!/usr/bin/env python3
"""Grok processes started as children of the MCP.

The kernel sends SIGCHLD to the parent when a child exits. This module is that
parent's reaper: the signal writes one byte to a pipe, and a single thread
wait()s the children. The MCP can signal a child's process group without
signaling itself, because each child is started in its own session.
"""

import os
import select
import signal
import threading


class Supervisor:
    def __init__(self):
        self.fallback_sec = 5.0
        self._procs = {}
        self._exits = {}
        self._events = {}
        self._lock = threading.Lock()
        self._thread = None
        self._installed = False
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        self._read_fd = read_fd
        self._write_fd = write_fd

    def install(self) -> None:
        """Listen for SIGCHLD. Call this from the MCP's main thread."""
        self._ensure_thread()
        if self._installed:
            return
        if threading.current_thread() is not threading.main_thread():
            return
        signal.signal(signal.SIGCHLD, self._on_signal)
        self._installed = True

    def _on_signal(self, signum, frame) -> None:
        try:
            os.write(self._write_fd, b"\0")
        except OSError:
            pass

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="grok-child-reaper", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            self.reap()
            try:
                readable, _, _ = select.select([self._read_fd], [], [], self.fallback_sec)
            except (OSError, ValueError):
                continue
            if not readable:
                continue
            try:
                os.read(self._read_fd, 256)
            except OSError:
                pass

    def track(self, proc) -> None:
        self._ensure_thread()
        event = threading.Event()
        with self._lock:
            self._procs[proc.pid] = proc
            self._events[proc.pid] = event
        self._on_signal(signal.SIGCHLD, None)

    def owns(self, pid) -> bool:
        if not isinstance(pid, int):
            return False
        with self._lock:
            return pid in self._procs or pid in self._exits

    def forget(self, pid) -> None:
        with self._lock:
            self._procs.pop(pid, None)
            self._exits.pop(pid, None)
            self._events.pop(pid, None)

    def reap(self) -> None:
        with self._lock:
            items = list(self._procs.items())
        for pid, proc in items:
            try:
                code = proc.poll()
            except OSError:
                continue
            if code is None:
                continue
            with self._lock:
                self._procs.pop(pid, None)
                self._exits[pid] = code
                event = self._events.get(pid)
            if event is not None:
                event.set()

    def wait(self, pid: int, timeout: float):
        """Return the child's exit code once the kernel has reported it."""
        self._ensure_thread()
        with self._lock:
            event = self._events.get(pid)
            if pid in self._exits:
                return self._exits[pid]
        if event is None:
            return None
        if not event.wait(timeout):
            with self._lock:
                return self._exits.get(pid)
        with self._lock:
            return self._exits.get(pid)

    def signal_group(self, proc, sig: int) -> bool:
        pid = getattr(proc, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.killpg(os.getpgid(pid), sig)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            try:
                os.kill(pid, sig)
                return True
            except OSError:
                return False

    def stop(self, proc) -> bool:
        return self.signal_group(proc, signal.SIGTERM)

    def kill(self, proc) -> bool:
        return self.signal_group(proc, signal.SIGKILL)


supervisor = Supervisor()
