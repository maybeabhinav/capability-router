# Agent adoption

Add this section to the instruction file used by your agent:

```md
## Capability Router

- Use the `capability` tool for optional skills and MCP tools.
- Search for one need per call. Set `limit` from 1 through 5.
- Search returns metadata only. Call `load_skill` before using a skill.
- For an MCP tool, call `describe` before its first `call`.
- Treat `policy_denied`, `unavailable`, `refresh_required`, and schema errors as stop signals.
```

Use the appropriate instruction file for your client:

- Codex: `AGENTS.md`
- Claude Code: `CLAUDE.md`

Restart the client after you add the MCP server, change its command, or refresh
the catalog. An active router process does not reload the catalog from disk.

## Recommended flow

```text
task
  -> capability(search)
  -> choose one exact ID
  -> capability(load_skill) or capability(describe)
  -> follow the skill or capability(call)
```

Discovery does not grant permission for external writes, messages,
deployments, or destructive actions. Keep those decisions in your agent's
normal authority rules.
