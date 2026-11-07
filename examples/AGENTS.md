# Codex coordinator

Use the `codex-orchestrator` skill in `skills/codex-orchestrator/`. Copy that
directory to `~/.codex/skills/codex-orchestrator` so Codex loads it.

The Codex session coordinates. Execution defaults to Grok through
`grok_delegate`. After the Grok plan is exhausted, the skill switches
`.codex/agent-routing.json` to Codex workers.
