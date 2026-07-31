from __future__ import annotations

import asyncio
import hashlib
import hmac
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from planning_platform.lifecycle.concurrency import run_sync_to_completion
from planning_platform.lifecycle.dedupe import (
    DeliveryClaim,
    InMemoryDeliveryDeduplicator,
)
from planning_platform.lifecycle.models import EventActor, EventEnvelope, EventSubject
from planning_platform.lifecycle.runners import LifecycleGateRejected, PublicationCommand
from planning_platform.lifecycle.webhooks import WebhookRejected, verified_webhook_envelope
from planning_platform.lifecycle.worker import execute_claimed_delivery, execute_delivery


def _envelope():
    raw = b'{"action":"opened"}'
    now = datetime(2026, 7, 31, tzinfo=UTC)
    return verified_webhook_envelope(
        source="github",
        event_type="github.pull_request",
        delivery_id="delivery-123",
        raw_body=raw,
        signature_header="sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest(),
        secret=b"secret",
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="service", id="github-app"),
        subject=EventSubject(idea_id=42),
    )


def test_verified_webhook_uses_exact_bytes_rejects_stale_delivery_and_deduplicates() -> None:
    event = _envelope()
    assert event.signature.verified
    assert event.idempotency_key.startswith("event:github:delivery-123:")
    memory = InMemoryDeliveryDeduplicator()
    now = event.received_at
    claim = memory.claim(event, now=now)
    assert claim.acquired and claim.claim_token is not None
    assert not memory.claim(event, now=now).acquired
    memory.complete(event, claim_token=claim.claim_token, now=now)
    assert memory.claim(event, now=now).state == "completed"
    raw = b"{}"
    with pytest.raises(WebhookRejected, match="stale"):
        verified_webhook_envelope(
            source="github",
            event_type="github.pull_request",
            delivery_id="old-delivery",
            raw_body=raw,
            signature_header="sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest(),
            secret=b"secret",
            occurred_at=now - timedelta(minutes=16),
            received_at=now,
            actor=EventActor(kind="service", id="github-app"),
            subject=EventSubject(),
        )
    with pytest.raises(WebhookRejected, match="does not match"):
        verified_webhook_envelope(
            source="github",
            event_type="github.pull_request",
            delivery_id="tampered-delivery",
            raw_body=b"{ }",
            signature_header="sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest(),
            secret=b"secret",
            occurred_at=now,
            received_at=now,
            actor=EventActor(kind="service", id="github-app"),
            subject=EventSubject(),
        )


def test_publication_command_requires_exact_merge_gate() -> None:
    envelope = type("Envelope", (), {"approved_commit": "a" * 40})()
    command = PublicationCommand(
        artifact=object(),  # type: ignore[arg-type]
        envelope=envelope,  # type: ignore[arg-type]
        approved_by_merge=True,
        merge_commit="a" * 40,
    )
    assert command.merge_commit == "a" * 40
    with pytest.raises(LifecycleGateRejected, match="merged"):
        PublicationCommand(
            artifact=object(),  # type: ignore[arg-type]
            envelope=envelope,  # type: ignore[arg-type]
            approved_by_merge=False,
            merge_commit="a" * 40,
        )
    with pytest.raises(LifecycleGateRejected, match="differ"):
        PublicationCommand(
            artifact=object(),  # type: ignore[arg-type]
            envelope=envelope,  # type: ignore[arg-type]
            approved_by_merge=True,
            merge_commit="b" * 40,
        )


@pytest.mark.asyncio
async def test_windmill_script_requires_a_verified_claim_and_never_replays_effect() -> None:
    event = _envelope()
    deduplicator = InMemoryDeliveryDeduplicator()
    effects: list[str] = []

    async def effect(_event: object) -> str:
        effects.append("ran")
        return "completed"

    assert await execute_delivery(event, deduplicator, effect) == "completed"
    assert await execute_delivery(event, deduplicator, effect) == "deduplicated:completed"
    assert effects == ["ran"]


@pytest.mark.asyncio
async def test_cancellation_during_claim_acquisition_releases_owned_fence() -> None:
    class BlockingClaimDeduplicator(InMemoryDeliveryDeduplicator):
        def __init__(self) -> None:
            super().__init__()
            self.acquired = threading.Event()
            self.return_claim = threading.Event()

        def claim(self, event: EventEnvelope, *, now: datetime) -> DeliveryClaim:
            claim = super().claim(event, now=now)
            self.acquired.set()
            self.return_claim.wait()
            return claim

    event = _envelope()
    deduplicator = BlockingClaimDeduplicator()
    effect_started = False

    async def effect(_event: object) -> str:
        nonlocal effect_started
        effect_started = True
        return "completed"

    task = asyncio.create_task(execute_delivery(event, deduplicator, effect))
    assert await asyncio.to_thread(deduplicator.acquired.wait, 1)
    task.cancel()
    deduplicator.return_claim.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not effect_started
    replacement = deduplicator.claim(event, now=datetime.now(UTC))
    assert replacement.acquired


@pytest.mark.asyncio
async def test_worker_cancellation_stops_effect_before_releasing_its_lease() -> None:
    event = _envelope()
    deduplicator = InMemoryDeliveryDeduplicator()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def effect(_event: object) -> str:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    task = asyncio.create_task(execute_delivery(event, deduplicator, effect))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    replacement = deduplicator.claim(event, now=datetime.now(UTC))
    assert replacement.acquired


@pytest.mark.asyncio
async def test_worker_cancellation_retains_lease_until_thread_effect_finishes() -> None:
    event = _envelope()
    deduplicator = InMemoryDeliveryDeduplicator()
    started = threading.Event()
    finish = threading.Event()
    finished = threading.Event()

    def blocking_effect() -> str:
        started.set()
        finish.wait()
        finished.set()
        return "completed"

    async def effect(_event: object) -> str:
        return await run_sync_to_completion(blocking_effect)

    task = asyncio.create_task(execute_delivery(event, deduplicator, effect))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not finished.is_set()
    replacement = deduplicator.claim(event, now=datetime.now(UTC))
    assert not replacement.acquired

    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    replacement = deduplicator.claim(event, now=datetime.now(UTC))
    assert replacement.acquired


@pytest.mark.asyncio
async def test_failed_heartbeat_fence_prevents_overlap_with_running_thread() -> None:
    class RenewalFailureDeduplicator(InMemoryDeliveryDeduplicator):
        def renew(self, *args: object, **kwargs: object):
            del args, kwargs
            raise RuntimeError("renewal unavailable")

    event = _envelope()
    deduplicator = RenewalFailureDeduplicator(lease=timedelta(seconds=1.2))
    started = threading.Event()
    finish = threading.Event()
    finished = threading.Event()

    def blocking_effect() -> str:
        started.set()
        finish.wait()
        finished.set()
        return "completed"

    async def effect(_event: object) -> str:
        return await run_sync_to_completion(blocking_effect)

    task = asyncio.create_task(execute_delivery(event, deduplicator, effect))
    assert await asyncio.to_thread(started.wait, 1)
    await asyncio.sleep(1.25)
    assert not task.done()
    assert not finished.is_set()
    replacement = deduplicator.claim(event, now=datetime.now(UTC))
    assert not replacement.acquired

    finish.set()
    with pytest.raises(RuntimeError, match="renewal unavailable"):
        await task
    assert finished.is_set()
    replacement = deduplicator.claim(event, now=datetime.now(UTC))
    assert replacement.acquired


@pytest.mark.asyncio
async def test_failed_dead_letter_recovery_returns_to_recoverable_state() -> None:
    event = _envelope()
    deduplicator = InMemoryDeliveryDeduplicator()
    now = datetime.now(UTC)
    original = deduplicator.claim(event, now=now)
    assert original.claim_token is not None
    deduplicator.dead_letter(
        event,
        claim_token=original.claim_token,
        reason="terminal",
        now=now,
    )
    recovery = deduplicator.recover_dead_letter(
        event,
        operator="test",
        reason="retry after repair",
        now=now,
    )

    async def failure(_event: object) -> str:
        raise RuntimeError("recovery still fails")

    with pytest.raises(RuntimeError, match="still fails"):
        await execute_claimed_delivery(
            event,
            deduplicator,
            failure,
            recovery,
            failure_mode="dead_letter",
        )
    second = deduplicator.recover_dead_letter(
        event,
        operator="test",
        reason="second bounded attempt",
        now=datetime.now(UTC),
    )
    assert second.acquired


def test_all_required_windmill_flows_are_git_synced_and_bounded() -> None:
    flow_root = Path(__file__).parents[1] / "windmill/f/planning"
    expected = {
        "idea_created",
        "idea_moved_to_planning",
        "planning_input_received",
        "planning_resume",
        "planning_artifacts_ready",
        "planning_pr_merged",
        "publish_openproject_graph",
        "openproject_work_package_changed",
        "github_pull_request_event",
        "github_check_run_event",
        "nightly_reconciliation",
        "replan_affected_subgraph",
        "dead_letter_recovery",
    }
    paths = tuple(flow_root.glob("*.flow/flow.yaml"))
    assert {path.parent.name.removesuffix(".flow") for path in paths} >= expected
    for path in paths:
        flow = yaml.safe_load(path.read_text())
        for module in flow["value"]["modules"]:
            retry = module.get("retry")
            if retry is None:
                continue
            attempts = sum(
                int(policy.get("attempts", 0))
                for policy in retry.values()
                if isinstance(policy, dict)
            )
            assert 1 <= attempts <= 4
        if path.parent.name != "dead_letter_recovery.flow":
            assert flow["value"].get("failure_module")
