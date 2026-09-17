# Capability Router

Capability Router exposes skills and MCP tools through one MCP tool named
`capability`. The model receives one stable schema. It searches for a
capability only when the task needs it.

The router supports independent personal, office, and project contexts. Each
agent session can select its own context. Many sessions can use the same
registry at the same time.

The runtime uses the Python standard library.

## Install

Capability Router requires Python 3.10 or newer on Linux or macOS.

```sh
pipx install git+https://github.com/maybeabhinav/capability-router.git
```

You can also use `uv`:

```sh
uv tool install git+https://github.com/maybeabhinav/capability-router.git
```

Create and refresh a read-only configuration:

```sh
capability-router init
capability-router refresh --config ~/.config/capability-router/config.json
```

## Connect one context

Create a context and select it for a unique session ID:

```sh
capability-router context create personal \
  --config ~/.config/capability-router/personal/config.json \
  --source personal \
  --source shared

capability-router context use personal --session codex-personal
```

Connect Claude Code:

```sh
claude mcp add --scope user capability-router -- \
  capability-router serve \
  --registry ~/.config/capability-router/contexts.json \
  --session claude-personal
```

Connect Codex:

```sh
codex mcp add capability-router -- \
  capability-router serve \
  --registry ~/.config/capability-router/contexts.json \
  --session codex-personal
```

Use a different session ID for each concurrent agent. One session can switch
contexts without changing another session.

See [Context management](docs/contexts.md) for context and account commands.

## Agent flow

The MCP server exposes one tool. Its common actions are:

1. `search` returns a small metadata result.
2. `load_skill` loads one selected skill.
3. `describe` returns one selected MCP tool schema.
4. `call` validates and invokes one selected MCP tool.
5. `context_current`, `context_list`, and `context_use` manage the
   current agent session.
6. `context_move`, `context_share`, `context_unassign`, and
   `context_undo` change capability assignments.

Search for one need per call. Use a result limit from 1 through 5.

The reusable agent skill is in
[skills/capability-router/SKILL.md](skills/capability-router/SKILL.md).

## Add skills

Pass one or more skill roots during setup:

```sh
capability-router init --force \
  --skill-root personal=~/.agents/skills \
  --skill-root shared=~/agent-skills
```

Each direct child can contain one `SKILL.md`. Refresh the catalog after a
skill changes.

## Add MCP servers

Add a server to the context configuration. Then refresh that configuration and
run `context doctor`.

A local stdio server:

```json
{
  "transport": "stdio",
  "command": "example-mcp-server",
  "args": [],
  "cwd": ".",
  "inherit_environment": ["PATH"],
  "environment_from_parent": {
    "EXAMPLE_API_TOKEN": "EXAMPLE_API_TOKEN"
  },
  "default_access": "unknown",
  "access_overrides": {
    "search": "read"
  }
}
```

A remote Streamable HTTP server:

```json
{
  "transport": "http",
  "url": "https://mcp.example.com/mcp",
  "headers_from_parent": {
    "Authorization": "EXAMPLE_AUTHORIZATION"
  },
  "default_access": "unknown",
  "access_overrides": {
    "search": "read"
  }
}
```

HTTP redirects are rejected. Remote URLs must use HTTPS. Loopback HTTP is
allowed for local development.

The current HTTP client supports JSON responses and SSE response frames. It
supports tokens supplied through private environment files. Interactive OAuth
login is not implemented yet.

## Private context environments

Keep values outside repository files. Create a JSON file with mode `0600`:

```json
{
  "EXAMPLE_API_TOKEN": "value",
  "GH_CONFIG_DIR": "/home/user/.config/gh-personal",
  "AWS_PROFILE": "personal"
}
```

Attach the file when you create the context:

```sh
capability-router context create personal \
  --config ~/.config/capability-router/personal/config.json \
  --environment-file ~/.config/capability-router/personal/environment.json
```

The router passes only variables named by a server configuration. A context
switch replaces the environment overlay for later calls.

Run a CLI with the same context:

```sh
capability-router context exec personal -- gh auth status
capability-router context exec office -- aws sts get-caller-identity
```

## Access classes

Classify each tool as `read`, `write`, `external_write`, `destructive`,
or `unknown`.

Read-only mode blocks every class except `read` before the downstream server
starts. Use unrestricted mode only when the calling agent has authority for
the requested operation.

## Verify

```sh
python3 -m unittest discover -s tests -v
python3 tests/verify_sdist.py
python3 -m compileall -q capability_router tests
```

The tests use local fixtures. They do not need external accounts.

See [Evaluation](docs/evaluation.md) for concurrency and agent behavior results.

## Scope

- Local stdio MCP servers
- Remote MCP Streamable HTTP servers with caller-supplied headers
- Local `SKILL.md` directories
- Per-session context selection
- Atomic context assignment revisions and undo
- Context-specific environment files
- MCP protocol versions `2024-11-05`, `2025-06-18`, and `2025-11-25`

## License

MIT
