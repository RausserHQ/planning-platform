"""Exclusive, connection-bound execution guards for planner mutations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .graph import build_planner_graph
from .model import PlanningModel


class ExecutionInProgress(RuntimeError):
    """Raised when another execution still owns the thread's hard fence."""


class ExecutionGuard(Protocol):
    def execution(self, thread_id: str) -> AbstractAsyncContextManager[Any]: ...


class InMemoryExecutionGuard:
    """Per-thread hard fence for tests using one shared in-memory graph."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    @asynccontextmanager
    async def execution(self, thread_id: str) -> AsyncIterator[Any]:
        async with self._registry_lock:
            lock = self._locks.setdefault(thread_id, asyncio.Lock())
            if lock.locked():
                raise ExecutionInProgress("planning thread already has an active graph execution")
            await lock.acquire()
        try:
            yield self._graph
        finally:
            lock.release()


class PostgresExecutionGuard:
    """Session advisory lock and saver bound to one non-reconnecting connection."""

    def __init__(
        self,
        database_url: str,
        model: PlanningModel,
        *,
        max_concurrency: int = 8,
    ) -> None:
        self._database_url = database_url
        self._model = model
        self._slots = asyncio.Semaphore(max_concurrency)

    @asynccontextmanager
    async def execution(self, thread_id: str) -> AsyncIterator[Any]:
        async with self._slots:
            connection = await AsyncConnection.connect(
                self._database_url,
                autocommit=True,
                prepare_threshold=0,
                row_factory=dict_row,
            )
            try:
                cursor = await connection.execute(
                    """
                    SELECT pg_try_advisory_lock(
                      hashtextextended(%s, 0::bigint)
                    ) AS acquired
                    """,
                    (thread_id,),
                )
                row = await cursor.fetchone()
                acquired = bool(row and row["acquired"])
                if not acquired:
                    raise ExecutionInProgress(
                        "planning thread already has an active graph execution"
                    )
                checkpointer = AsyncPostgresSaver(connection)
                yield build_planner_graph(self._model, checkpointer)
            finally:
                await _close_connection(connection)


async def _close_connection(connection: AsyncConnection[Any]) -> None:
    """Close the lock-owning session fully, even under repeated cancellation."""
    close_task = asyncio.create_task(connection.close())
    cancelled = False
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            cancelled = True
    await close_task
    if cancelled:
        raise asyncio.CancelledError
