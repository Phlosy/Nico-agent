"""Exact-version Tool Executor registry."""

from __future__ import annotations

from collections.abc import Iterable

from nico_agent.tools.contracts import ToolDefinitionSpec, ToolExecutor
from nico_agent.tools.errors import ToolNotFound, ToolRegistryConflict


class ToolRegistry:
    def __init__(self, executors: Iterable[ToolExecutor] = ()) -> None:
        self._executors: dict[tuple[str, str], ToolExecutor] = {}
        for executor in executors:
            self.register(executor)

    def register(self, executor: ToolExecutor) -> None:
        key = (executor.spec.name, executor.spec.version)
        if key in self._executors:
            raise ToolRegistryConflict(*key)
        if len(executor.implementation_hash) != 64:
            raise ValueError("tool implementation_hash must be a SHA-256 hex digest")
        try:
            int(executor.implementation_hash, 16)
        except ValueError as exc:
            raise ValueError("tool implementation_hash must be a SHA-256 hex digest") from exc
        self._executors[key] = executor

    def get(self, name: str, version: str) -> ToolExecutor:
        try:
            return self._executors[(name, version)]
        except KeyError as exc:
            raise ToolNotFound(name, version) from exc

    def definitions(self) -> tuple[ToolDefinitionSpec, ...]:
        return tuple(
            executor.spec
            for _, executor in sorted(
                self._executors.items(), key=lambda item: (item[0][0], item[0][1])
            )
        )
