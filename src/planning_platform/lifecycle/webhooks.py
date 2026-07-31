"""Webhook verification over exact raw bytes, followed by bounded normalization."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import (
    EventActor,
    EventEnvelope,
    EventSource,
    EventSubject,
    EventType,
    VerifiedSignature,
    envelope_for_delivery,
)

MAX_WEBHOOK_BYTES = 1_048_576
MAX_DELIVERY_AGE = timedelta(minutes=15)
MAX_FUTURE_SKEW = timedelta(minutes=2)


class WebhookRejected(ValueError):
    """An unauthenticated, malformed, stale, or oversized webhook."""


def _exact_hmac(raw_body: bytes, header: str | None, secret: bytes, algorithm: str) -> None:
    if not raw_body or len(raw_body) > MAX_WEBHOOK_BYTES:
        raise WebhookRejected("webhook body is empty or exceeds the permitted size")
    if not isinstance(header, str):
        raise WebhookRejected("webhook signature is missing")
    prefix, separator, supplied = header.partition("=")
    expected_prefix = "sha256" if algorithm == "sha256" else "sha1"
    expected_length = 64 if algorithm == "sha256" else 40
    if separator != "=" or prefix != expected_prefix or len(supplied) != expected_length:
        raise WebhookRejected("webhook signature is malformed")
    expected = hmac.new(secret, raw_body, getattr(hashlib, algorithm)).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise WebhookRejected("webhook signature does not match raw request bytes")


def _payload(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WebhookRejected("webhook body is not a JSON object") from error
    if not isinstance(payload, dict):
        raise WebhookRejected("webhook body must be a JSON object")
    return payload


def reject_stale_delivery(
    occurred_at: datetime,
    *,
    now: datetime,
    maximum_age: timedelta = MAX_DELIVERY_AGE,
) -> None:
    """Reject old and future deliveries before any lifecycle side effect starts."""
    if occurred_at.tzinfo is None or now.tzinfo is None:
        raise WebhookRejected("delivery timestamps must be timezone aware")
    occurred = occurred_at.astimezone(UTC)
    reference = now.astimezone(UTC)
    if occurred < reference - maximum_age or occurred > reference + MAX_FUTURE_SKEW:
        raise WebhookRejected("webhook delivery is stale or implausibly future-dated")


def verified_webhook_envelope(
    *,
    source: EventSource,
    event_type: EventType,
    delivery_id: str,
    raw_body: bytes,
    signature_header: str | None,
    secret: bytes,
    occurred_at: datetime,
    received_at: datetime,
    actor: EventActor,
    subject: EventSubject,
    key_id: str | None = None,
) -> EventEnvelope:
    """Verify raw HMAC bytes and return the only event form scripts may consume."""
    if not delivery_id or len(delivery_id) > 255:
        raise WebhookRejected("webhook delivery ID is invalid")
    algorithm = "sha256" if source == "github" else "sha1"
    expected_source = "github" if algorithm == "sha256" else "openproject"
    if source != expected_source:
        raise WebhookRejected("only GitHub and OpenProject webhooks use this verifier")
    _exact_hmac(raw_body, signature_header, secret, algorithm)
    reject_stale_delivery(occurred_at, now=received_at)
    payload = _payload(raw_body)
    return envelope_for_delivery(
        event_type=event_type,
        source=source,
        delivery_id=delivery_id,
        occurred_at=occurred_at,
        received_at=received_at,
        actor=actor,
        subject=subject,
        signature=VerifiedSignature(
            verified=True,
            algorithm=("github-hmac-sha256" if source == "github" else "openproject-hmac-sha1"),
            key_id=key_id,
        ),
        payload=payload,
    )
