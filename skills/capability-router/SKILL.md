---
name: capability-router
description: Use and maintain Capability Router when an agent must discover a skill or MCP tool, switch personal or office context, change capability assignments, add an MCP server, or diagnose router configuration.
---

# Capability Router

Use the single `capability` MCP tool for optional skills and MCP tools.

## Use a capability

1. Call `search` for one need. Set `limit` from 1 through 5.
2. Select one exact capability ID.
3. Call `load_skill` for a skill.
4. Call `describe` before the first call to an MCP tool.
5. Call `call` with arguments that match the returned schema.

Stop on `policy_denied`, `unavailable`, `refresh_required`, or schema
errors. Do not retry a completed external action.

## Use contexts

Call `context_current` before work that depends on an account or work domain.
Call `context_use` to change only this session.

Use `context_explain` when a capability is missing. Use
`context_move`, `context_share`, or `context_unassign` only when the user
asked to change assignments. Include the current registry revision when the
operation supports `expected_revision`.

For CLI account selection, run:

```sh
capability-router context exec CONTEXT -- COMMAND
```

## Extend the router

Read [references/operations.md](references/operations.md) before you add an
MCP server, create a context, change private environment files, or diagnose a
failed refresh.
