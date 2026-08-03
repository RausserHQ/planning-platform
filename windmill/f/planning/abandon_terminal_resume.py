"""Abandon one terminal delivery's unfinished planner resume without replaying it."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from planning_platform.lifecycle.runtime import LifecycleRuntime


async def _main_async(
    plan_id: str,
    plan_version: int,
    thread_id: str,
    idempotency_key: str,
    operator: str,
    reason: str,
) -> dict[str, Any]:
    async with LifecycleRuntime.from_environment() as runtime:
        outcome = await runtime.service.abandon_terminal_resume(
            plan_id=plan_id,
            plan_version=plan_version,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            operator=operator,
            reason=reason,
        )
    return asdict(outcome)


def main(
    plan_id: str,
    plan_version: int,
    thread_id: str,
    idempotency_key: str,
    operator: str,
    reason: str,
) -> dict[str, Any]:
    # fmt: off
    return asyncio.run(_main_async(plan_id, plan_version, thread_id, idempotency_key, operator, reason))  # noqa: E501
    # fmt: on
