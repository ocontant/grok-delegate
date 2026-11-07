# Codex coordinator: Grok workers

Use this when a Codex session should hand work to the local Grok CLI through
the `grok_delegate` MCP server.

## When Grok is selected

Call `delegate` with:

- `task`: a self-contained prompt. Include the role, the objective, the
  constraints, and what the worker must report.
- `cwd`: the absolute working directory for that task.
- `wait`: `false` for each member of a parallel batch, then `delegate_wait`
  on the returned job id. `true` waits for that one job. The wait does not
  block the next MCP call.

Omit `agent`. The working directory is the context. Two directories run at
the same time. The same directory is busy only while its Grok child is alive.

Do not start a second worker for a context that `pool` still shows as running.
Do not call the `codex` CLI from the worker prompt.

## While a worker runs

- `bus_publish` sends a message to that context id (`to`, for example `w3`).
- `bus_receive` reads messages the workers sent the coordinator.
- `bus_streams` shows each context's status, job, and last message.
- `pool` lists contexts and whether each Grok pid is alive.

The worker is told its bus name and talks back with `bus.py`.
