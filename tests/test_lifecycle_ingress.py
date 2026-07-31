from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest

from planning_platform.lifecycle.ingress import github_envelope
from planning_platform.lifecycle.webhooks import WebhookRejected


def _event(payload: dict[str, object], *, body: dict[str, object] | None = None):
    raw = json.dumps(payload, separators=(",", ":"))
    signature = hmac.new(b"secret", raw.encode(), hashlib.sha256).hexdigest()
    return {
        "raw_string": raw,
        "body": payload if body is None else body,
        "headers": {
            "x-github-delivery": "delivery-1176",
            "x-github-event": "pull_request",
            "x-hub-signature-256": f"sha256={signature}",
        },
    }


def test_ingress_derives_identity_and_freshness_only_from_signed_raw_bytes() -> None:
    now = datetime(2026, 7, 30, 19, 0, tzinfo=UTC)
    payload: dict[str, object] = {
        "pull_request": {
            "number": 1,
            "updated_at": "2026-07-30T18:59:00Z",
        },
        "sender": {"id": 42},
    }
    envelope = github_envelope(_event(payload), secret=b"secret", received_at=now)
    assert envelope.payload == payload
    assert envelope.actor.id == "42"
    assert envelope.occurred_at == datetime(2026, 7, 30, 18, 59, tzinfo=UTC)

    mismatched = {
        **payload,
        "pull_request": {
            "number": 1,
            "updated_at": "2026-07-30T19:00:00Z",
        },
    }
    with pytest.raises(WebhookRejected, match="differs"):
        github_envelope(
            _event(payload, body=mismatched),
            secret=b"secret",
            received_at=now,
        )


def test_ingress_rejects_missing_resource_timestamp_instead_of_using_receipt_time() -> None:
    payload: dict[str, object] = {
        "pull_request": {"number": 1},
        "sender": {"id": 42},
    }
    with pytest.raises(WebhookRejected, match="timestamp"):
        github_envelope(
            _event(payload),
            secret=b"secret",
            received_at=datetime(2026, 7, 30, 19, 0, tzinfo=UTC),
        )
