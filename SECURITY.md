# Security

## Report a vulnerability

Use GitHub's private vulnerability reporting feature for this repository. Do
not include credentials or private data in a public issue.

## Operating model

- Start with `mode: "read-only"`.
- Classify every MCP tool before relying on policy enforcement.
- Keep secret values outside the router configuration.
- Pass only required environment variables to each downstream server.
- Review a server before adding its executable and arguments.
- Refresh the catalog after skills or downstream tool schemas change.

The router validates a tool schema again before each call. It refuses the call
when the runtime schema differs from the catalog.
