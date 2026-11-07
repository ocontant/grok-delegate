"""Isolated checks for the pool, the bus, and the MCP tool list.

These tests never start the Grok CLI. Point state at a temp directory before
importing the bridge modules.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_STATE = tempfile.mkdtemp(prefix="grok-delegate-test-")
os.environ["GROK_DELEGATE_STATE"] = _STATE
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bus  # noqa: E402
import mcp_server  # noqa: E402
import pool  # noqa: E402
import run  # noqa: E402


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.left = tempfile.mkdtemp(prefix="ctx-a-")
        self.right = tempfile.mkdtemp(prefix="ctx-b-")

    def test_two_directories_are_two_contexts(self):
        first, first_error = pool.acquire(None, Path(self.left))
        second, second_error = pool.acquire(None, Path(self.right))
        self.assertIsNone(first_error)
        self.assertIsNone(second_error)
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue(pool.release(first["id"], first["job"]))
        self.assertTrue(pool.release(second["id"], second["job"]))

    def test_a_held_context_is_not_issued_twice(self):
        worker, error = pool.acquire(None, Path(self.left))
        self.assertIsNone(error)
        again, again_error = pool.acquire(None, Path(self.left))
        self.assertIsNotNone(again_error)
        self.assertEqual(again["id"], worker["id"])
        self.assertTrue(pool.release(worker["id"], worker["job"]))

    def test_mark_running_then_idle_records_the_session(self):
        worker, error = pool.acquire(None, Path(self.left))
        self.assertIsNone(error)
        self.assertTrue(pool.mark_running(worker["id"], os.getpid(), "job-1", worker["job"]))
        idle = pool.mark_idle(worker["id"], session_id="session-1", pid=os.getpid(), job="job-1")
        self.assertEqual(idle["status"], "idle")
        self.assertEqual(idle["sessionId"], "session-1")
        self.assertIsNone(idle["pid"])


class BusTest(unittest.TestCase):
    def test_publish_lands_on_the_recipient_queue(self):
        sent = bus.publish("coord", "w9", "hello", kind="message", job="job-9")
        inbox = bus.receive("w9", limit=5, consume=True)
        self.assertEqual(inbox[-1]["id"], sent["id"])
        self.assertEqual(inbox[-1]["text"], "hello")
        self.assertEqual(inbox[-1]["from"], "coord")
        self.assertEqual(bus.receive("w9", consume=True), [])


class McpTest(unittest.TestCase):
    def test_initialize_and_tool_list(self):
        init = mcp_server._invoke(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertEqual(init["result"]["serverInfo"]["name"], "grok-delegate")
        listed = mcp_server._invoke({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(
            names,
            {"delegate", "delegate_wait", "pool", "bus_publish", "bus_receive", "bus_streams"},
        )

    def test_pool_tool_returns_json(self):
        reply = mcp_server._invoke(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "pool", "arguments": {}},
            }
        )
        text = reply["result"]["content"][0]["text"]
        self.assertIsInstance(json.loads(text), list)

    def test_serve_refuses_a_public_address(self):
        self.assertEqual(mcp_server.serve("0.0.0.0:9"), 1)


class PromptTest(unittest.TestCase):
    def test_worker_prompt_points_at_this_trees_bus(self):
        bus_cli = Path(run.__file__).resolve().parent / "bus.py"
        text = run._preamble()
        self.assertIn(f"python3 {bus_cli} pub", text)
        self.assertIn(f"python3 {bus_cli} sub", text)


if __name__ == "__main__":
    unittest.main()
