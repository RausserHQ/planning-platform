"""Normalize Windmill v2 trigger events into verified lifecycle envelopes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from .models import (
    EventActor,
    EventEnvelope,
    EventSubject,
    VerifiedSignature,
    envelope_for_delivery,
)
from .webhooks import WebhookRejected, verified_webhook_envelope


def _timestamp(value: object, label: str) -> datetime:
    if isinstance(value, int):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    raise WebhookRejected(f"{label} must be an explicit timezone-aware timestamp")


def _raw_event(event: dict[str, Any]) -> tuple[bytes, dict[str, str], dict[str, Any]]:
    raw = event.get("raw_string")
    headers_value = event.get("headers")
    if not isinstance(raw, str) or not isinstance(headers_value, dict):
        raise WebhookRejected("Windmill trigger omitted exact raw body or headers")
    headers = {
        str(key).casefold(): str(value)
        for key, value in headers_value.items()
        if isinstance(key, str) and isinstance(value, (str, int, float, bool))
    }
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise WebhookRejected("Windmill trigger raw body is not JSON") from error
    if not isinstance(parsed, dict):
        raise WebhookRejected("Windmill trigger body must be an object")
    body = event.get("body")
    if body is not None and body != parsed:
        raise WebhookRejected("Windmill parsed body differs from the signed raw body")
    return raw.encode("utf-8"), headers, parsed


def github_envelope(
    event: dict[str, Any],
    *,
    secret: bytes,
    received_at: datetime | None = None,
) -> EventEnvelope:
    raw, headers, payload = _raw_event(event)
    now = received_at or datetime.now(UTC)
    delivery = headers.get("x-github-delivery")
    kind = headers.get("x-github-event")
    if kind not in {"pull_request", "check_run"}:
        raise WebhookRejected("unsupported GitHub webhook event")
    if not delivery:
        raise WebhookRejected("GitHub delivery ID is absent")
    resource = payload.get(kind)
    if not isinstance(resource, dict):
        raise WebhookRejected("GitHub event resource is absent")
    occurred = _timestamp(
        resource.get("updated_at")
        or resource.get("completed_at")
        or resource.get("started_at")
        or resource.get("created_at"),
        "GitHub resource timestamp",
    )
    sender = payload.get("sender")
    actor_id = (
        str(sender.get("id") or sender.get("login")) if isinstance(sender, dict) else "github"
    )
    return verified_webhook_envelope(
        source="github",
        event_type=("github.pull_request" if kind == "pull_request" else "github.check_run"),
        delivery_id=delivery,
        raw_body=raw,
        signature_header=headers.get("x-hub-signature-256"),
        secret=secret,
        occurred_at=occurred,
        received_at=now,
        actor=EventActor(kind="service", id=actor_id),
        subject=EventSubject(),
        key_id="github-app-webhook",
    )


def openproject_envelope(
    event: dict[str, Any],
    *,
    secret: bytes,
    trusted_service_actor_ids: Collection[str],
    received_at: datetime | None = None,
) -> EventEnvelope:
    trusted_actors = frozenset(trusted_service_actor_ids)
    if not trusted_actors or any(not value for value in trusted_actors):
        raise ValueError("trusted OpenProject service actor IDs are required")
    raw, headers, payload = _raw_event(event)
    now = received_at or datetime.now(UTC)
    action = payload.get("action")
    if not isinstance(action, str):
        raise WebhookRejected("OpenProject webhook action is absent")
    is_comment = action.startswith("work_package_comment:")
    key = "work_package_comment" if is_comment else "work_package"
    resource = payload.get(key)
    if not isinstance(resource, dict):
        raise WebhookRejected("OpenProject webhook resource is absent")
    occurred = _timestamp(
        resource.get("updatedAt") or resource.get("createdAt"),
        "OpenProject resource timestamp",
    )
    delivery = headers.get("x-openproject-delivery") or hashlib.sha256(raw).hexdigest()
    actor = payload.get("actor")
    actor_value = actor.get("id") if isinstance(actor, dict) else None
    if not isinstance(actor_value, (int, str)) or not str(actor_value):
        raise WebhookRejected("OpenProject webhook actor identity is absent")
    actor_id = str(actor_value)
    idea_id: int | None = None
    if not is_comment and type(resource.get("id")) is int:
        idea_id = resource["id"]
    if is_comment:
        links = resource.get("_links")
        work_package = links.get("workPackage") if isinstance(links, dict) else None
        href = work_package.get("href") if isinstance(work_package, dict) else None
        if isinstance(href, str) and href.rstrip("/").rsplit("/", 1)[-1].isdigit():
            idea_id = int(href.rstrip("/").rsplit("/", 1)[-1])
    return verified_webhook_envelope(
        source="openproject",
        event_type=(
            "openproject.idea_comment" if is_comment else "openproject.work_package_changed"
        ),
        delivery_id=delivery,
        raw_body=raw,
        signature_header=headers.get("x-op-signature"),
        secret=secret,
        occurred_at=occurred,
        received_at=now,
        actor=EventActor(
            kind=("service" if actor_id in trusted_actors else "human"),
            id=actor_id,
        ),
        subject=EventSubject(idea_id=idea_id),
        key_id="openproject-webhook",
    )


def scheduled_envelope(
    *,
    delivery_id: str,
    occurred_at: datetime | None = None,
) -> EventEnvelope:
    now = occurred_at or datetime.now(UTC)
    return envelope_for_delivery(
        event_type="reconciliation.scheduled",
        source="scheduler",
        delivery_id=delivery_id,
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="system", id="windmill-scheduler"),
        subject=EventSubject(),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={"schedule": "nightly_reconciliation"},
    )
