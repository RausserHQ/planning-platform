"""Explicit planner schema migration; never called by application startup."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .idempotency import PostgresIdempotencyRepository


async def migrate(database_url: str) -> None:
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=2,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
    )
    await pool.open()
    try:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        await PostgresIdempotencyRepository(pool).setup()
    finally:
        await pool.close()


def main() -> None:
    database_url = os.environ.get("PLANNER_DATABASE_URL")
    if not database_url:
        raise SystemExit("PLANNER_DATABASE_URL is required")
    asyncio.run(migrate(database_url))


if __name__ == "__main__":
    main()
