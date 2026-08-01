"""Execute and fail closed on one trusted read-only convergence proof."""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict
from typing import Any

from planning_platform.lifecycle.ingress import convergence_check_envelope
from planning_platform.lifecycle.models import EventEnvelope
from planning_platform.lifecycle.runtime import LifecycleRuntime
from planning_platform.lifecycle.service import LifecycleOutcome
from planning_platform.lifecycle.worker import execute_delivery


def _trusted_envelope(event: dict[str, Any]) -> EventEnvelope:
    envelope = EventEnvelope.model_validate(event)
    stable_delivery = os.environ.get("WM_ROOT_FLOW_JOB_ID") or os.environ.get("WM_JOB_ID")
    if not stable_delivery:
        raise RuntimeError("Windmill job identity is required for convergence proof")
    expected = convergence_check_envelope(
        plan_id=envelope.subject.plan_id or "",
        plan_version=envelope.subject.plan_version or 0,
        delivery_id=stable_delivery,
        occurred_at=envelope.occurred_at,
    )
    if envelope != expected:
        raise RuntimeError("convergence envelope is not bound to this Windmill root job")
    return envelope


def _windmill_result(
    event: EventEnvelope,
    result: LifecycleOutcome | str,
) -> dict[str, Any] | str:
    if isinstance(result, str):
        return {
            "wm_failure": "convergence_result_unavailable",
            "event": event.model_dump(mode="json"),
            "result": result,
        }
    serialized = asdict(result)
    if result.outcome == "zero_operations":
        return serialized
    return {
        "wm_failure": result.outcome,
        "event": event.model_dump(mode="json"),
        "result": serialized,
    }


async def _main_async(event: dict[str, Any]) -> dict[str, Any] | str:
    envelope = _trusted_envelope(event)
    async with LifecycleRuntime.from_environment() as runtime:
        result = await execute_delivery(
            envelope,
            runtime.deduplicator,
            runtime.service.handle,
        )
    return _windmill_result(envelope, result)


def main(event: dict[str, Any]) -> dict[str, Any] | str:
    return asyncio.run(_main_async(event))
