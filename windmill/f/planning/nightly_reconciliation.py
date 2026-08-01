"""Windmill scheduled entrypoint for full deterministic reconciliation."""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict

from planning_platform.lifecycle.ingress import scheduled_envelope
from planning_platform.lifecycle.runtime import LifecycleRuntime
from planning_platform.lifecycle.worker import execute_delivery


async def _main_async(delivery_id: str | None = None) -> dict[str, object] | str:
    stable_delivery = (
        delivery_id or os.environ.get("WM_ROOT_FLOW_JOB_ID") or os.environ.get("WM_JOB_ID")
    )
    if not stable_delivery:
        raise RuntimeError("Windmill job identity is required for reconciliation")
    event = scheduled_envelope(delivery_id=f"nightly:{stable_delivery}")
    async with LifecycleRuntime.from_environment() as runtime:
        result = await execute_delivery(
            event,
            runtime.deduplicator,
            runtime.service.handle,
        )
    return asdict(result) if not isinstance(result, str) else result


def main(delivery_id: str | None = None) -> dict[str, object] | str:
    return asyncio.run(_main_async(delivery_id))
