# Context management

A context selects capabilities, a router configuration, and an optional private
environment file. The registry stores paths and assignments. It does not store
credential values.

## Create and inspect

```sh
capability-router context create personal \
  --config ~/.config/capability-router/personal/config.json \
  --source personal \
  --source shared \
  --environment-file ~/.config/capability-router/personal/environment.json

capability-router context list
capability-router context doctor
capability-router context history --limit 20
```

## Select per session

```sh
capability-router context use personal --session agent-01
capability-router context current --session agent-01
```

Use a unique session ID for each Claude or Codex process.

## Change assignments

Move one capability:

```sh
capability-router context move linear.search \
  --from office \
  --to personal
```

Share it with another context:

```sh
capability-router context share shared.review --to office
```

Hide it in one context:

```sh
capability-router context unassign slack.send --context personal
```

Each write returns a registry revision and change ID. Use
`--expected-revision` when an agent must reject a concurrent stale write.
Undo the latest compatible change with:

```sh
capability-router context undo CHANGE_ID
```

Undo fails if a later registry change makes the recorded state stale.

## Run account-specific CLIs

```sh
capability-router context exec personal -- gh auth status
capability-router context exec office -- gcloud auth list
```

Put login selectors such as `GH_CONFIG_DIR`, `AWS_PROFILE`,
`CLOUDSDK_CONFIG`, and `KUBECONFIG` in the context environment file.

## Agent actions

An agent connected with registry session mode can call:

- `context_list`
- `context_current`
- `context_use`
- `context_explain`
- `context_move`
- `context_share`
- `context_unassign`
- `context_undo`

The agent should read the returned revision before a shared registry write.
