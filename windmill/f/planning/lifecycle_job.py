"""Windmill entrypoint for one previously verified lifecycle delivery."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from planning_platform.lifecycle.models import EventEnvelope
from planning_platform.lifecycle.runtime import LifecycleRuntime
from planning_platform.lifecycle.worker import execute_delivery


async def main(event: dict[str, Any]) -> dict[str, Any] | str:
    envelope = EventEnvelope.model_validate(event)
    async with LifecycleRuntime.from_environment() as runtime:
        result = await execute_delivery(
            envelope,
            runtime.deduplicator,
            runtime.service.handle,
        )
    return asdict(result) if not isinstance(result, str) else result
