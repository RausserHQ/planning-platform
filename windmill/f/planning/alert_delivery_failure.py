"""Record sanitized terminal alert-delivery failures without replaying effects."""

from __future__ import annotations

import os
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from planning_platform.alert_delivery import (
    AlertDeliveryRejected,
    alertmanager_envelope,
    operational_alert_payload_sha256,
)
from planning_platform.lifecycle.store import PostgresLifecycleStore


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value


def main(
    event: dict[str, Any],
    error: dict[str, Any] | None = None,
) -> dict[str, object]:
    del error
    recorded = 0
    try:
        envelope = alertmanager_envelope(
            event,
            token=_required("ALERTMANAGER_WEBHOOK_TOKEN"),
        )
    except AlertDeliveryRejected:
        pass
    else:
        store = PostgresLifecycleStore(_required("PLANNING_LIFECYCLE_DATABASE_URL"))
        trace_id = str(uuid5(NAMESPACE_URL, f"planning-alert-group:{envelope.group_key}"))
        try:
            failure_job_id = UUID(_required("WM_JOB_ID"))
        except ValueError as job_id_error:
            raise RuntimeError("WM_JOB_ID must be a UUID") from job_id_error
        for alert_index, alert in enumerate(envelope.alerts):
            payload_sha256 = operational_alert_payload_sha256(alert)
            event_id = str(
                uuid5(
                    failure_job_id,
                    f"{alert_index}:{alert.fingerprint}:{payload_sha256}",
                )
            )
            store.audit(
                event_id=event_id,
                trace_id=trace_id,
                action="alert_delivery",
                outcome="failed",
                details={
                    "fingerprint": alert.fingerprint,
                    "alertname": alert.name,
                    "state": alert.state,
                    "severity": alert.severity,
                    "payload_sha256": payload_sha256,
                },
            )
        recorded = len(envelope.alerts)
    return {
        "windmill_status_code": 503,
        "windmill_content_type": "application/json",
        "result": {
            "recorded": recorded,
            "retryable": True,
        },
    }
