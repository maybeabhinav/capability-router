"""Context-aware executor for one MCP session."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
import weakref
from typing import Any

from .catalog import load_catalog
from .config import Config, load_config
from .contexts import (
    ContextView,
    RegistryStore,
    current_context,
    explain_availability,
    explain_context,
    list_contexts,
    move_capability,
    select_context,
    share_capability,
    unassign_capability,
    undo_change,
)
from .executor import Executor, validate_action
from .environment import load_private_environment
from .errors import InputError
from .protocol import RouterAction


CONTEXT_ACTIONS = frozenset(
    {
        RouterAction.CONTEXT_LIST,
        RouterAction.CONTEXT_CURRENT,
        RouterAction.CONTEXT_USE,
        RouterAction.CONTEXT_EXPLAIN,
        RouterAction.CONTEXT_MOVE,
        RouterAction.CONTEXT_UNDO,
        RouterAction.CONTEXT_SHARE,
        RouterAction.CONTEXT_UNASSIGN,
    }
)


def _filtered_catalog(
    config: Config,
    view: ContextView,
) -> dict[str, Any]:
    catalog, _ = load_catalog(config)
    capabilities = [
        item
        for item in catalog["capabilities"]
        if explain_availability(view, item)[0]
    ]
    return {**catalog, "capabilities": capabilities}


class ContextRuntime:
    """Own the active executor for one client session."""

    def __init__(
        self,
        store: RegistryStore,
        session: str,
        *,
        run_id: str,
        audit_path: Path | None,
    ) -> None:
        self.store = store
        self.session = session
        self.run_id = run_id
        self.audit_path = audit_path
        self.lock = threading.RLock()
        self.retired: weakref.WeakSet[Executor] = weakref.WeakSet()
        selected = current_context(store, session)
        self.context = selected["context"]
        self.registry_revision = selected["current_registry_revision"]
        self.executor = self._build(self.context)

    def _build(self, context: str) -> Executor:
        registry = self.store.read()
        document = registry["contexts"].get(context)
        if document is None:
            raise InputError(f"context {context} does not exist")
        view = ContextView.from_document(context, document)
        config = load_config(view.config_path)
        if view.environment_file is not None:
            try:
                environment = load_private_environment(view.environment_file)
            except ValueError as error:
                raise InputError(str(error)) from error
            config = replace(config, environment_overrides=environment)
        catalog = _filtered_catalog(config, view)
        self.registry_revision = registry["revision"]
        return Executor(
            config,
            catalog,
            run_id=self.run_id,
            audit_path=self.audit_path,
        )

    @property
    def config(self) -> Config:
        with self.lock:
            return self.executor.config

    def _replace(self, context: str) -> None:
        replacement = self._build(context)
        self.retired.add(self.executor)
        self.executor = replacement
        self.context = context

    def execute(self, raw: Any) -> dict[str, Any]:
        arguments = validate_action(raw)
        action = arguments["action"]
        if action not in CONTEXT_ACTIONS:
            with self.lock:
                executor = self.executor
            return executor.execute(raw)
        handlers = {
            RouterAction.CONTEXT_LIST: self._list,
            RouterAction.CONTEXT_CURRENT: self._current,
            RouterAction.CONTEXT_USE: self._use,
            RouterAction.CONTEXT_EXPLAIN: self._explain,
            RouterAction.CONTEXT_MOVE: self._move,
            RouterAction.CONTEXT_UNDO: self._undo,
            RouterAction.CONTEXT_SHARE: self._share,
            RouterAction.CONTEXT_UNASSIGN: self._unassign,
        }
        return handlers[action](arguments)

    def _list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return list_contexts(self.store)

    def _current(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            return {
                "context": self.context,
                "session": self.session,
                "registry_revision": self.registry_revision,
                "capability_count": len(self.executor.catalog["capabilities"]),
            }

    def _use(self, arguments: dict[str, Any]) -> dict[str, Any]:
        context = arguments["context"]
        with self.lock:
            replacement = self._build(context)
            selected = select_context(self.store, context, self.session)
            self.retired.add(self.executor)
            self.executor = replacement
            self.context = context
            self.registry_revision = selected["registry_revision"]
            return {
                **selected,
                "capability_count": len(replacement.catalog["capabilities"]),
            }

    def _explain(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return explain_context(
            self.store,
            arguments["capability_id"],
            arguments["context"],
        )

    def _move(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            result = move_capability(
                self.store,
                arguments["capability_id"],
                arguments["source_context"],
                arguments["target_context"],
                arguments.get("expected_revision"),
            )
            if self.context in {
                arguments["source_context"],
                arguments["target_context"],
            }:
                self._replace(self.context)
            return result

    def _share(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            result = share_capability(
                self.store,
                arguments["capability_id"],
                arguments["target_context"],
                arguments.get("expected_revision"),
            )
            if self.context == arguments["target_context"]:
                self._replace(self.context)
            return result

    def _unassign(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            result = unassign_capability(
                self.store,
                arguments["capability_id"],
                arguments["context"],
                arguments.get("expected_revision"),
            )
            if self.context == arguments["context"]:
                self._replace(self.context)
            return result

    def _undo(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            result = undo_change(self.store, arguments["change_id"])
            self._replace(self.context)
            return result

    def known_capability_id(self, value: Any) -> str | None:
        with self.lock:
            return self.executor.known_capability_id(value)

    def audit(self, *args: Any, **kwargs: Any) -> None:
        with self.lock:
            executor = self.executor
        executor.audit(*args, **kwargs)

    def store_artifact(self, document: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            executor = self.executor
        return executor.store_artifact(document)

    def close_active_clients(self) -> None:
        with self.lock:
            executors = {self.executor, *self.retired}
        for executor in executors:
            executor.close_active_clients()
