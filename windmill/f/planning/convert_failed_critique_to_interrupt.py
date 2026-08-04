"""Convert one exact historical exhausted critique failure into human input."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from typing import Any

from planning_platform.lifecycle.runtime import LifecycleRuntime


async def _main_async(
    plan_id: str,
    plan_version: int,
    thread_id: str,
    idempotency_key: str,
    interrupt_id: str,
    comment_id: int,
    comment_created_at: str,
    operator: str,
    reason: str,
) -> dict[str, Any]:
    async with LifecycleRuntime.from_environment() as runtime:
        outcome = await runtime.service.convert_failed_critique_to_interrupt(
            plan_id=plan_id,
            plan_version=plan_version,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            interrupt_id=interrupt_id,
            comment_id=comment_id,
            comment_created_at=datetime.fromisoformat(
                comment_created_at.replace("Z", "+00:00")
            ),
            operator=operator,
            reason=reason,
        )
    return asdict(outcome)


def main(
    plan_id: str,
    plan_version: int,
    thread_id: str,
    idempotency_key: str,
    interrupt_id: str,
    comment_id: int,
    comment_created_at: str,
    operator: str,
    reason: str,
) -> dict[str, Any]:
    # fmt: off
    return asyncio.run(_main_async(plan_id, plan_version, thread_id, idempotency_key, interrupt_id, comment_id, comment_created_at, operator, reason))  # noqa: E501
    # fmt: on
