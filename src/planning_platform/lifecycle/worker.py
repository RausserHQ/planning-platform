"""Retry-safe execution wrapper shared by versioned Windmill scripts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal

from .concurrency import acquire_sync_owned, run_sync_to_completion
from .dedupe import DeliveryClaim, DeliveryDeduplicator
from .models import EventEnvelope


async def execute_delivery[Result](
    event: EventEnvelope,
    deduplicator: DeliveryDeduplicator,
    effect: Callable[[EventEnvelope], Awaitable[Result]],
) -> Result | str:
    """Lease one delivery, retry failures, and complete only after the effect."""
    if not event.signature.verified:
        raise ValueError("Windmill lifecycle jobs require a verified event envelope")

    def cancel_claim(acquired: DeliveryClaim) -> None:
        if not acquired.acquired or acquired.claim_token is None:
            return
        try:
            deduplicator.retry(
                event,
                claim_token=acquired.claim_token,
                reason="claim acquisition cancelled",
                now=datetime.now(UTC),
            )
        finally:
            deduplicator.release_fence(
                event,
                claim_token=acquired.claim_token,
            )

    claim = await acquire_sync_owned(
        deduplicator.claim,
        cancel_claim,
        event,
        now=datetime.now(UTC),
    )
    if not claim.acquired:
        return f"deduplicated:{claim.state}"
    return await execute_claimed_delivery(event, deduplicator, effect, claim)


async def execute_claimed_delivery[Result](
    event: EventEnvelope,
    deduplicator: DeliveryDeduplicator,
    effect: Callable[[EventEnvelope], Awaitable[Result]],
    claim: DeliveryClaim,
    *,
    failure_mode: Literal["retry", "dead_letter"] = "retry",
) -> Result:
    """Execute under a token-fenced claim and renew it until the effect ends."""
    if not claim.acquired or claim.claim_token is None or claim.lease_expires_at is None:
        raise ValueError("delivery effect requires an acquired claim token")
    claim_token = claim.claim_token
    initial_seconds = (claim.lease_expires_at - datetime.now(UTC)).total_seconds()
    heartbeat_seconds = max(1.0, min(60.0, initial_seconds / 3))

    async def heartbeat() -> Result:
        while True:
            await asyncio.sleep(heartbeat_seconds)
            await run_sync_to_completion(
                deduplicator.renew,
                event,
                claim_token=claim_token,
                now=datetime.now(UTC),
            )

    effect_task: asyncio.Future[Result] = asyncio.ensure_future(effect(event))
    heartbeat_task: asyncio.Task[Result] = asyncio.create_task(heartbeat())
    try:
        try:
            done, _pending = await asyncio.wait(
                {effect_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                error = heartbeat_task.exception()
                if error is None:
                    raise RuntimeError("delivery heartbeat stopped unexpectedly")
                raise error
            result = effect_task.result()
        except BaseException as error:
            effect_task.cancel()
            await asyncio.gather(effect_task, return_exceptions=True)
            with suppress(ValueError):
                if failure_mode == "dead_letter":
                    await run_sync_to_completion(
                        deduplicator.restore_dead_letter,
                        event,
                        claim_token=claim_token,
                        reason=f"recovery failed: {type(error).__name__}",
                        now=datetime.now(UTC),
                    )
                else:
                    await run_sync_to_completion(
                        deduplicator.retry,
                        event,
                        claim_token=claim_token,
                        reason=type(error).__name__,
                        now=datetime.now(UTC),
                    )
            raise
        await run_sync_to_completion(
            deduplicator.complete,
            event,
            claim_token=claim_token,
            now=datetime.now(UTC),
        )
        return result
    finally:
        if not heartbeat_task.done():
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        await run_sync_to_completion(
            deduplicator.release_fence,
            event,
            claim_token=claim_token,
        )
