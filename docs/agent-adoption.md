# Agent adoption

Add this section to the instruction file used by the agent:

```md
## Capability Router

- Use the `capability` tool for optional skills and MCP tools.
- Search for one need per call. Set `limit` from 1 through 5.
- Search returns metadata only. Call `load_skill` before using a skill.
- Call `describe` before the first call to an MCP tool.
- Call `context_current` before account-specific or domain-specific work.
- Use `context_use` to switch only the current agent session.
- Treat `policy_denied`, `unavailable`, `refresh_required`, and schema errors as stop signals.
```

Use `AGENTS.md` for Codex and `CLAUDE.md` for Claude Code.

Use a unique router session ID for each concurrent agent process. Restart the
client after you change its MCP command. The agent can switch a registry-backed
router between contexts without restarting it.

## Recommended flow

```text
task
  -> capability(context_current), when account or domain matters
  -> capability(search)
  -> choose one exact ID
  -> capability(load_skill) or capability(describe)
  -> follow the skill or capability(call)
```

## Codex approval mode

The router has one MCP tool for read and write operations. Codex cannot assign
different approval modes to actions inside one tool. Set the router server to
`approve` only when its context and access configuration is the accepted trust
boundary:

```toml
[mcp_servers.capability-router]
default_tools_approval_mode = "approve"
```

Keep downstream access classes accurate. Use router read-only mode for sessions
that must not call write, external-write, destructive, or unknown tools.
