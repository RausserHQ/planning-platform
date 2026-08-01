"""Normalized, replay-safe event contracts for lifecycle automation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class LifecycleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


EventType = Literal[
    "github.check_run",
    "github.pull_request",
    "openproject.idea_comment",
    "openproject.work_package_changed",
    "planning.artifacts_ready",
    "planning.convergence_check",
    "planning.replan_affected_subgraph",
    "planning.pr_merged",
    "reconciliation.scheduled",
    "windmill.dead_letter_replayed",
]
EventSource = Literal["github", "openproject", "scheduler", "windmill"]
SignatureAlgorithm = Literal["github-hmac-sha256", "openproject-hmac-sha1", "internal", "none"]


class EventActor(LifecycleModel):
    kind: Literal["human", "service", "system"]
    id: str = Field(min_length=1, max_length=255)


class EventSubject(LifecycleModel):
    idea_id: int | None = Field(default=None, ge=1)
    plan_id: str | None = Field(default=None, max_length=64)
    plan_version: int | None = Field(default=None, ge=1)
    node_key: str | None = Field(default=None, max_length=255)


class VerifiedSignature(LifecycleModel):
    verified: bool
    algorithm: SignatureAlgorithm
    key_id: str | None = Field(default=None, max_length=255)


class EventEnvelope(LifecycleModel):
    """The code representation of event-envelope schema v1.0.0."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    event_id: UUID
    event_type: EventType
    source: EventSource
    source_delivery_id: str = Field(min_length=1, max_length=255)
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    trace_id: UUID
    idempotency_key: str = Field(pattern=r"^[a-z0-9][a-z0-9:._/-]{7,255}$")
    actor: EventActor
    subject: EventSubject
    signature: VerifiedSignature
    payload: dict[str, Any]


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_payload_sha256(payload: dict[str, Any]) -> str:
    """Hash a normalized payload without ever persisting/logging its raw wire bytes."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def envelope_for_delivery(
    *,
    event_type: EventType,
    source: EventSource,
    delivery_id: str,
    occurred_at: datetime,
    received_at: datetime,
    actor: EventActor,
    subject: EventSubject,
    signature: VerifiedSignature,
    payload: dict[str, Any],
) -> EventEnvelope:
    """Give every delivery a stable event, trace, and idempotency identity."""
    seed = f"{source}:{delivery_id}"
    digest = canonical_payload_sha256(payload)
    return EventEnvelope(
        event_id=uuid5(NAMESPACE_URL, f"event:{seed}"),
        event_type=event_type,
        source=source,
        source_delivery_id=delivery_id,
        occurred_at=occurred_at,
        received_at=received_at,
        trace_id=uuid5(NAMESPACE_URL, f"trace:{seed}"),
        idempotency_key=f"event:{source}:{delivery_id}:{digest[:16]}",
        actor=actor,
        subject=subject,
        signature=signature,
        payload=payload,
    )
