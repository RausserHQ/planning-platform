"""Operator-authorized transition and replay for one trusted dead letter."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from planning_platform.lifecycle.concurrency import (
    acquire_sync_owned,
    run_sync_to_completion,
)
from planning_platform.lifecycle.dedupe import DeliveryClaim
from planning_platform.lifecycle.models import EventEnvelope
from planning_platform.lifecycle.runtime import LifecycleRuntime
from planning_platform.lifecycle.worker import execute_claimed_delivery


async def _main_async(
    event: dict[str, Any],
    operator: str,
    reason: str,
) -> dict[str, Any] | str:
    envelope = EventEnvelope.model_validate(event)
    async with LifecycleRuntime.from_environment() as runtime:

        def cancel_recovery(acquired: DeliveryClaim) -> None:
            if not acquired.acquired or acquired.claim_token is None:
                return
            try:
                runtime.deduplicator.restore_dead_letter(
                    envelope,
                    claim_token=acquired.claim_token,
                    reason="operator recovery claim acquisition cancelled",
                    now=datetime.now(UTC),
                )
            finally:
                runtime.deduplicator.release_fence(
                    envelope,
                    claim_token=acquired.claim_token,
                )

        claim = await acquire_sync_owned(
            runtime.deduplicator.recover_dead_letter,
            cancel_recovery,
            envelope,
            operator=operator,
            reason=reason,
            now=datetime.now(UTC),
        )

        async def audited_replay(trusted: EventEnvelope):
            await run_sync_to_completion(
                runtime.store.audit,
                event_id=str(trusted.event_id),
                trace_id=str(trusted.trace_id),
                action="dead_letter_recovery",
                outcome="operator_authorized",
                details={"operator": operator, "reason": reason},
            )
            return await runtime.service.handle(trusted)

        result = await execute_claimed_delivery(
            envelope,
            runtime.deduplicator,
            audited_replay,
            claim,
            failure_mode="dead_letter",
        )
    return asdict(result)


def main(
    event: dict[str, Any],
    operator: str,
    reason: str,
) -> dict[str, Any] | str:
    return asyncio.run(_main_async(event, operator, reason))
