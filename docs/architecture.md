# KoreAgent Next backend architecture

The backend keeps the JSON-RPC contract stable in `application.py` and places
behavior in three focused packages.

## Module map

```text
kore_next/
  application.py          Composition root and JSON-RPC facade
  agent.py                Compatibility exports for older imports
  rpc.py                  RpcFault and parameter validation
  model/
    client.py             Retry policy, credentials, protocol dispatch
    types.py              ModelReply and usage normalization
    presets.py            Provider labels, official hosts, API-style selection
    openai_chat.py        OpenAI-compatible Chat Completions
    openai_responses.py   OpenAI Responses API
    anthropic_messages.py Anthropic Messages API
  tools/
    definitions.py        Canonical tool schemas
    executor.py           Tool activation, approval, files, shell, web
    shell.py              Local/container shell argv construction
  services/
    conversations.py      Conversation persistence and search
    runs.py               Run records, event journal, recovery, traces
    run_engine.py         Agent loop orchestration
    run_postprocessing.py Context summaries, reflection, plan extraction
    model_settings.py     Provider configuration and role overrides
    approvals.py          Human approval requests and persistent rules
    artifacts.py          File and URL deliverables
    automations.py        Scheduling, occurrence claims, restart recovery
    skills.py             Skill discovery and lifecycle
    mcp_settings.py       MCP configuration and runtime presentation
    extensions.py         Extension import workflow and service facade
    tool_router.py        Routes service-backed tools to owning services
```

## Main call chains

```text
server WebSocket RPC
  -> Application.methods()
  -> feature service

conversation.send
  -> ConversationService.add_message
  -> RunStore.create
  -> RunEngine.execute
  -> ContextManager.prepare / prepare_step
  -> ModelClient.complete
  -> protocol adapter
  -> ToolExecutor.execute
  -> ServiceToolRouter.call
  -> RunStore.event / CheckpointStore.save

automation tick
  -> AutomationService.loop
  -> AutomationService.claim_occurrence
  -> Application._execute_run
  -> RunEngine.execute
```

`Application` owns shared runtime state (`running`, approval waiters, broadcast,
background tasks) and injects it into services. Services receive settings and
mutable runtime dependencies through callables so host restarts and tests can
replace them without rebuilding the whole object graph.

## Compatibility boundary

`kore_next.agent` still exports `ModelClient`, `ModelReply`, `ToolExecutor`,
`builtin_tool_definitions`, and `shell_command_argv`. Existing callers do not
need to change imports, while new code should import from `kore_next.model` and
`kore_next.tools`.
