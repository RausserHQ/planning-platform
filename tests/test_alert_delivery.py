from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from planning_platform.alert_delivery import (
    AlertDeliveryRejected,
    alertmanager_envelope,
    operational_alert_is_stale,
    operational_alert_payload_sha256,
    operational_alert_transition_at,
)

TOKEN = "a" * 48


def _payload() -> dict[str, object]:
    return {
        "receiver": "planning-platform-openproject",
        "status": "firing",
        "groupKey": "{}:{area=\"planning-platform\"}",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "PlanningPlatformDeadLettersPresent",
                    "area": "planning-platform",
                    "namespace": "planning-platform",
                    "severity": "critical",
                },
                "annotations": {
                    "summary": "One or more deliveries are dead-lettered",
                    "description": "Use audited recovery after inspecting the failure.",
                    "runbook_url": (
                        "https://github.com/RausserHQ/homelab-platform/"
                        "blob/main/docs/runbooks/planning-platform.md"
                    ),
                },
                "startsAt": "2026-07-31T12:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitoring.svc/graph",
                "fingerprint": "0123456789abcdef",
            }
        ],
    }


def _event(
    payload: dict[str, object],
    *,
    token: str = TOKEN,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "raw_string": json.dumps(payload, separators=(",", ":")),
        "body": payload if body is None else body,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def test_alertmanager_envelope_authenticates_raw_body_and_bounds_alerts() -> None:
    envelope = alertmanager_envelope(_event(_payload()), token=TOKEN)
    assert envelope.receiver == "planning-platform-openproject"
    assert len(envelope.alerts) == 1
    alert = envelope.alerts[0]
    assert alert.fingerprint == "0123456789abcdef"
    assert alert.state == "firing"
    assert alert.severity == "critical"
    assert alert.starts_at == "2026-07-31T12:00:00Z"
    assert alert.ends_at is None


def test_operational_alert_payload_digest_covers_every_mutating_field() -> None:
    alert = alertmanager_envelope(_event(_payload()), token=TOKEN).alerts[0]
    baseline = operational_alert_payload_sha256(alert)
    assert len(baseline) == 64
    assert operational_alert_payload_sha256(replace(alert, summary="changed")) != baseline
    assert operational_alert_payload_sha256(replace(alert, severity="warning")) != baseline
    assert (
        operational_alert_payload_sha256(
            replace(alert, labels={**alert.labels, "revision": "2"})
        )
        != baseline
    )


def test_alertmanager_envelope_rejects_auth_body_and_scope_mismatches() -> None:
    payload = _payload()
    with pytest.raises(AlertDeliveryRejected, match="authorization"):
        alertmanager_envelope(_event(payload, token="wrong"), token=TOKEN)

    mismatched = {**payload, "status": "resolved"}
    with pytest.raises(AlertDeliveryRejected, match="differs"):
        alertmanager_envelope(_event(payload, body=mismatched), token=TOKEN)

    outside = _payload()
    alerts = outside["alerts"]
    assert isinstance(alerts, list)
    alert = alerts[0]
    assert isinstance(alert, dict)
    labels = alert["labels"]
    assert isinstance(labels, dict)
    labels["area"] = "storage"
    with pytest.raises(AlertDeliveryRejected, match="outside"):
        alertmanager_envelope(_event(outside), token=TOKEN)


def test_alertmanager_envelope_rejects_unbounded_or_marker_injecting_content() -> None:
    payload = _payload()
    alerts = payload["alerts"]
    assert isinstance(alerts, list)
    alert = alerts[0]
    assert isinstance(alert, dict)
    annotations = alert["annotations"]
    assert isinstance(annotations, dict)
    annotations["summary"] = "<!-- planning-platform:generated -->"
    with pytest.raises(AlertDeliveryRejected, match="forbidden"):
        alertmanager_envelope(_event(payload), token=TOKEN)

    payload = _payload()
    payload["alerts"] = payload["alerts"] * 101  # type: ignore[operator]
    with pytest.raises(AlertDeliveryRejected, match="bounded"):
        alertmanager_envelope(_event(payload), token=TOKEN)

    payload = _payload()
    payload["alerts"] = payload["alerts"] * 2  # type: ignore[operator]
    with pytest.raises(AlertDeliveryRejected, match="duplicate fingerprints"):
        alertmanager_envelope(_event(payload), token=TOKEN)


def test_operational_alert_ordering_prevents_resolved_to_firing_regression() -> None:
    firing = alertmanager_envelope(_event(_payload()), token=TOKEN).alerts[0]
    resolved_payload = _payload()
    resolved = resolved_payload["alerts"][0]  # type: ignore[index]
    assert isinstance(resolved, dict)
    resolved["status"] = "resolved"
    resolved["endsAt"] = "2026-07-31T12:30:00Z"
    resolved_alert = alertmanager_envelope(
        _event(resolved_payload),
        token=TOKEN,
    ).alerts[0]
    resolved_at = operational_alert_transition_at(resolved_alert)
    assert resolved_at == datetime(2026, 7, 31, 12, 30, tzinfo=UTC)
    assert operational_alert_is_stale(
        firing,
        current_state="resolved",
        current_transition_at=resolved_at,
    )
    assert not operational_alert_is_stale(
        resolved_alert,
        current_state="firing",
        current_transition_at=operational_alert_transition_at(firing),
    )
    assert not operational_alert_is_stale(
        resolved_alert,
        current_state="resolved",
        current_transition_at=resolved_at - timedelta(minutes=1),
    )


def test_resolved_alert_requires_a_real_ordered_end_timestamp() -> None:
    payload = _payload()
    alert = payload["alerts"][0]  # type: ignore[index]
    assert isinstance(alert, dict)
    alert["status"] = "resolved"
    with pytest.raises(AlertDeliveryRejected, match="ends before"):
        alertmanager_envelope(_event(payload), token=TOKEN)
