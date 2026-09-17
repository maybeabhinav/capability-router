# Router operations

## Add an MCP server

1. Identify the context that owns the server.
2. Add the server to that context's router configuration or named server
   source.
3. Classify every known tool access level. Keep the default as `unknown`.
4. Put credential values in the context environment file. Keep mode `0600`.
5. Run `capability-router refresh --config CONFIG`.
6. Run `capability-router context doctor`.
7. Search, describe, and call one read-only operation through a fresh router
   session.

Use `stdio` for a local child process. Use `http` for a remote Streamable
HTTP endpoint. HTTP authentication headers map to environment variable names
through `headers_from_parent`.

The built-in HTTP transport does not perform interactive OAuth login. Use a
pinned local OAuth bridge when the provider requires browser login. Configure
the bridge as a stdio server. Use an absolute executable path.

Give each context its own private OAuth store. Pass the store path through a
context environment file with mode `0600`. Keep the store directory at mode
`0700`. Do not copy tokens between contexts.

The first discovery or call can open a browser. Complete login once for each
service and context. Then refresh the context configuration so the authenticated
tools enter the catalog. Run `context doctor` and call one read operation.

## Create a context

Create a separate router configuration when server definitions, skill roots,
or access modes differ. Reuse one configuration when only capability
assignments or environment values differ.

Use a unique agent session ID. Do not use one global current-context file.

## Diagnose

Run:

```sh
capability-router context doctor
capability-router context list
capability-router context history --limit 20
```

For one missing capability, use `context_explain`. For a stale catalog,
refresh the owning configuration and restart or reconnect the MCP process.

Do not print environment file contents, bearer tokens, OAuth tokens, or secret
values.
