# Evaluation

## Local contract suite

The release suite has 112 public-interface tests. It covers catalog discovery,
schema validation, policy denial, process cleanup, output limits, context
revisions, session switching, environment isolation, and stdio and HTTP
transports.

The source distribution runs the same 112 tests after extraction.

## Concurrent session check

A local fixture run started 23 router sessions against one registry. Every
session switched to its own selected context and called the same read-only MCP
tool.

| Measure | Result |
| --- | ---: |
| Successful sessions | 23 of 23 |
| Read tool calls | 23 |
| Wall time | 0.782 s |
| Mean session latency | 0.588 s |
| p95 session latency | 0.737 s |
| Leaked downstream processes | 0 |

This check proves local session isolation and process cleanup. It does not
prove that an external provider accepts 23 concurrent requests. Provider
quotas still apply.

## Real-agent behavior check

Claude Code and Codex were each restricted to the router and local fixture.
Both agents found and called the read capability. The router denied the write
capability before the fixture received it.

The earlier exact-sequence evaluator incorrectly failed Codex because Codex
called `describe` before `call`. That step follows the documented contract.
Future scoring accepts valid optional inspection steps and grades the observed
result.

## Paired token checks

Use blinded paired runs for direct-tool and router configurations:

- Give both variants the same organic task.
- Do not use evaluation terms in candidate prompts or paths.
- Give each variant its normal adoption instructions.
- Keep model, account, working tree, and task fixed.
- Grade tool traces and output artifacts. Do not grade agent self-report.
- Present variants to a judge under neutral labels.
- Record uncached input, cached input, output, turns, tool calls, latency, and
  task success.
- Reject a token comparison when either variant did not invoke its configured
  tool path.

A first paired run was rejected because the router variants did not receive
the adoption instruction and did not invoke the router. Its token counts are
not evidence for or against the router.
