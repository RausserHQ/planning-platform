"""Terminal Windmill flow failure hook with durable dead-letter visibility."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from planning_platform.lifecycle.concurrency import (
    acquire_sync_owned,
    run_sync_to_completion,
)
from planning_platform.lifecycle.dedupe import DeliveryClaim
from planning_platform.lifecycle.models import EventEnvelope
from planning_platform.lifecycle.runtime import LifecycleRuntime


def _handler_result(
    result: dict[str, str],
    preserve_failure: bool,
) -> dict[str, Any]:
    if not preserve_failure:
        return result
    return {
        "wm_failure": "convergence_check_failed",
        "result": result,
    }


async def main(
    event: dict[str, Any],
    error: dict[str, Any] | None = None,
    preserve_failure: bool = False,
) -> dict[str, Any]:
    try:
        envelope = EventEnvelope.model_validate(event)
    except ValueError:
        # An invalid signature can fail in the verifier before a trusted
        # envelope exists. Keep Windmill's native failure record, but never
        # persist attacker-controlled input in the lifecycle dead-letter table.
        return _handler_result(
            {
                "state": "rejected",
                "message": "unverified delivery was not persisted",
            },
            preserve_failure,
        )
    safe_error = error if isinstance(error, dict) else {}
    name = str(safe_error.get("name") or safe_error.get("error") or "WindmillFlowError")
    step_id = str(safe_error.get("step_id") or safe_error.get("step") or "unknown")
    reason = f"{name}:{step_id}"[:1_024]
    async with LifecycleRuntime.from_environment() as runtime:

        def cancel_claim(acquired: DeliveryClaim) -> None:
            if not acquired.acquired or acquired.claim_token is None:
                return
            try:
                runtime.deduplicator.retry(
                    envelope,
                    claim_token=acquired.claim_token,
                    reason="dead-letter claim acquisition cancelled",
                    now=datetime.now(UTC),
                )
            finally:
                runtime.deduplicator.release_fence(
                    envelope,
                    claim_token=acquired.claim_token,
                )

        claim = await acquire_sync_owned(
            runtime.deduplicator.claim,
            cancel_claim,
            envelope,
            now=datetime.now(UTC),
        )
        if claim.state == "completed":
            return _handler_result(
                {"state": "completed", "message": "delivery already completed"},
                preserve_failure,
            )
        if claim.state == "dead_letter":
            return _handler_result(
                {"state": "dead_letter", "message": "delivery already dead-lettered"},
                preserve_failure,
            )
        if not claim.acquired or claim.claim_token is None:
            raise RuntimeError("delivery lease is still active during terminal failure")
        await run_sync_to_completion(
            runtime.deduplicator.dead_letter,
            envelope,
            claim_token=claim.claim_token,
            reason=reason,
            now=datetime.now(UTC),
        )
        await run_sync_to_completion(
            runtime.store.audit,
            event_id=str(envelope.event_id),
            trace_id=str(envelope.trace_id),
            action="dead_letter",
            outcome=reason,
            details={"error_name": name, "step_id": step_id},
        )
    # Windmill already retains the full terminal error. Do not echo an
    # arbitrary error message that could contain request or secret material.
    return _handler_result(
        {"state": "dead_letter", "message": "terminal failure recorded"},
        preserve_failure,
    )
