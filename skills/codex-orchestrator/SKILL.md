---
name: codex-orchestrator
description: >
  Route a Codex coordinator so Grok does the execution and Codex plan
  limits stay available for coordination. Use when starting work, choosing
  a worker backend, reading .codex/agent-routing.json, calling grok_delegate,
  or when a Grok worker reports that its plan or usage allowance is exhausted.
---

# Codex orchestrator

The Codex session coordinates. Grok executes. Codex workers execute only after the Grok plan is exhausted, so the Codex plan lasts long enough to keep driving Grok.

## Routing file

Before the first execution task, read the `.codex/agent-routing.json` that applies to the task directory. The nearest file wins. A linked worktree with no file of its own uses the file at the owning repository. Create `<workspace>/.codex/agent-routing.json` when no file exists.

Execution starts on Grok:

```json
{
  "worker_backend": "grok",
  "grok_delegate_enabled": true
}
```

`grok_delegate_enabled` is true only when `worker_backend` is `grok`. Any other pair is an invalid file: report it and fix it before delegating. Leave `worker_backend: ruflo` as written and use Ruflo for that project.

A user instruction in the current session to use a named backend overrides this default for that session. Record the conflict when it disagrees with the file.

## While Grok is selected

Keep this session on coordination: split the work, write the task, review the result, and answer the user. Do the execution through the `grok_delegate` MCP tools.

- `delegate` with a self-contained `task`, the absolute `cwd`, and `wait: false` for each member of a parallel batch. Then `delegate_wait` on each job id. Use `wait: true` for one job. Omit `agent`.
- The `cwd` is the context. A second task for a context that `pool` shows as running waits or uses another directory.
- `bus_publish`, `bus_receive`, and `bus_streams` carry messages while the workers run.
- Pass each worker the role, the objective, the constraints, and the result the coordinator needs. Pass earlier findings forward. The worker cannot see this session.

The hook refuses native Codex worker launches while this file says `grok`. Do not work around that refusal. Do not do the implementation in this session.

## When the Grok plan is exhausted

Switch only when the delegate result, or the worker's reply, says the Grok plan, usage allowance, or rate window is exhausted.

These results stay on Grok:

- The task failed, a test failed, or the worker is blocked on a decision.
- The context is busy.
- The bridge reports `error_max_turns` or the turn ceiling. Split the task or raise `GROK_DELEGATE_MAX_TURNS`.
- The `grok` binary, the bridge, or the MCP server is unavailable. Report that blocker.

On plan exhaustion:

1. Leave running Grok jobs alone. Do not start a second worker for their work.
2. Write the routing file to Codex before any Codex worker starts:

```json
{
  "worker_backend": "codex",
  "grok_delegate_enabled": false
}
```

3. Tell the user that Grok's plan is exhausted and that further execution uses Codex workers.
4. Start those workers with the native Codex collaboration tools. Give each one the same self-contained task the Grok worker would have received.

Codex workers draw on the same plan as this coordinator. Spend them on execution only in this overflow window.

## When Grok is available again

When the user says the Grok plan is available again, write the Grok routing file back before the next execution task. Leave Codex workers that are already running. New execution goes to Grok.
