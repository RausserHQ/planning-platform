"""Cancellation-safe bridges for synchronous lifecycle dependencies."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def run_sync_to_completion[Result](
    function: Callable[..., Result],
    /,
    *args: object,
    **kwargs: object,
) -> Result:
    """Run a synchronous effect without abandoning it when its task is cancelled.

    Python cannot stop a function that is already running in a worker thread.
    Shielding the worker and delaying cancellation until it finishes keeps the
    delivery lease heartbeat alive and prevents another worker from overlapping
    a still-running external mutation.
    """
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancellation_requested = False
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError:
            cancellation_requested = True
            if worker.cancelled():
                raise
            if worker.done():
                result = worker.result()
                break
    if cancellation_requested:
        raise asyncio.CancelledError
    return result


async def acquire_sync_owned[Result](
    function: Callable[..., Result],
    cancel_cleanup: Callable[[Result], object],
    /,
    *args: object,
    **kwargs: object,
) -> Result:
    """Acquire synchronous ownership and clean it up if cancellation wins.

    Unlike an ordinary synchronous call, a claim may create a durable row and
    hold a session advisory lock before its return value crosses the async
    boundary. This helper guarantees the returned ownership token is either
    delivered to the caller or passed to the supplied cleanup.
    """
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancellation_requested = False
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError:
            cancellation_requested = True
            if worker.cancelled():
                raise
            if worker.done():
                result = worker.result()
                break
    if cancellation_requested:
        try:
            await run_sync_to_completion(cancel_cleanup, result)
        finally:
            raise asyncio.CancelledError
    return result
