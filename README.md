# Capability Router

Capability Router gives an agent one MCP tool named `capability`. The agent can
search a local catalog, load one skill, inspect one MCP tool, and call only the
selected tool.

This reduces the tool schemas and skill text placed in every model request.
The router uses only the Python standard library at runtime.

## Install

Requires Python 3.10 or newer on Linux or macOS. Windows users can run it in
WSL.

With `pipx`:

```sh
pipx install git+https://github.com/maybeabhinav/capability-router.git
```

With `uv`:

```sh
uv tool install git+https://github.com/maybeabhinav/capability-router.git
```

Create a safe starter configuration:

```sh
capability-router init
capability-router refresh --config ~/.config/capability-router/config.json
```

The starter configuration is read-only. It discovers skills from the common
Claude Code, Codex, and agent skill directories. Missing directories are
ignored.

## Connect an agent

Claude Code:

```sh
claude mcp add --scope user capability-router -- \
  capability-router serve --config ~/.config/capability-router/config.json
```

Codex:

```sh
codex mcp add capability-router -- \
  capability-router serve --config ~/.config/capability-router/config.json
```

For Codex, let the router tool run without a second approval prompt. The router
still applies its own access policy:

```toml
[mcp_servers.capability-router]
default_tools_approval_mode = "approve"
```

Add the short rule from [Agent adoption](https://github.com/maybeabhinav/capability-router/blob/main/docs/agent-adoption.md) to your agent
instructions. Start a fresh agent process after changing MCP configuration.

## How an agent uses it

The public MCP surface has one tool and five actions:

1. `search` finds a small set of matching skills or MCP tools.
2. `load_skill` returns the selected `SKILL.md`.
3. `describe` returns the selected MCP tool schema and access class.
4. `call` validates arguments and invokes that MCP tool once.
5. `status` reports catalog state.

Search returns metadata only. It does not load a skill. Agents should search
for one need per call and use a result limit from 1 through 5.

## Add skills

Pass one or more skill directories during setup:

```sh
capability-router init --force \
  --skill-root personal=~/.agents/skills \
  --skill-root team=~/work/agent-skills
```

Each direct child directory can contain one `SKILL.md` file. Run `refresh`
after a skill changes. Then restart the client or reconnect its MCP server.
An active router process keeps the catalog that it loaded at startup.

## Add MCP servers

Edit `~/.config/capability-router/config.json`. Add local `stdio` servers under
`servers`. See the [configuration example](https://github.com/maybeabhinav/capability-router/blob/main/examples/config.json).

Classify each tool with `default_access` or `access_overrides`:

- `read`
- `write`
- `external_write`
- `destructive`
- `unknown`

In `read-only` mode, the router blocks every class except `read` before it
starts the downstream server. Use `unrestricted` mode only when the agent's
normal approval and authority rules permit writes.

The router passes only explicitly allowlisted environment variables to child
servers. Keep credential values outside the configuration file.

## Private environment files

`capability-router-env` can load a JSON environment file before it starts the
router. The file must use mode `0600`.

```sh
capability-router-env \
  --env-file ~/.config/capability-router/private-env.json \
  serve --config ~/.config/capability-router/config.json
```

Example private file:

```json
{
  "EXAMPLE_API_TOKEN": "value-stored-outside-git"
}
```

## Development

```sh
python3 -m unittest discover -s tests -v
python3 tests/verify_sdist.py
python3 -m compileall -q capability_router tests
```

The contract tests start local fixture MCP servers. They do not need network
access or external accounts.

## Current scope

- Local `stdio` MCP servers
- Local `SKILL.md` directories
- MCP protocol versions `2024-11-05`, `2025-06-18`, and `2025-11-25`
- JSON Schema validation for the supported subset tested in this repository

Remote HTTP MCP transport is outside the current scope.

## License

MIT
