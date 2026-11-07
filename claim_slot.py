#!/usr/bin/env python3
"""Show the contexts and grok pids the MCP owns. This does not register a terminal."""

import json
from pathlib import Path


def claim(cwd: Path, session_id: str) -> dict:
    """Older callers import this. It does not write a pin."""
    del cwd, session_id
    return {
        "ok": True,
        "registered": False,
        "note": "grok_delegate starts each Grok as its child and indexes that pid by context.",
    }


def main() -> int:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pool import snapshot

    workers = snapshot()
    running = [worker for worker in workers if worker.get("alive")]
    print(json.dumps({
        "ok": True,
        "registered": False,
        "running": len(running),
        "contexts": workers,
        "note": "No terminal was registered. A context is busy only while its grok pid is alive.",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
