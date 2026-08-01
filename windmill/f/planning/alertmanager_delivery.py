"""Deliver one authenticated Alertmanager webhook through the deterministic adapter."""

from __future__ import annotations

import os
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg

from planning_platform.alert_delivery import (
    alertmanager_envelope,
    operational_alert_is_stale,
    operational_alert_payload_sha256,
    operational_alert_transition_at,
)
from planning_platform.lifecycle.store import PostgresLifecycleStore
from planning_platform.openproject_transport import discover_openproject_adapter


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value


def main(event: dict[str, Any]) -> dict[str, object]:
    envelope = alertmanager_envelope(
        event,
        token=_required("ALERTMANAGER_WEBHOOK_TOKEN"),
    )
    database_url = _required("PLANNING_LIFECYCLE_DATABASE_URL")
    openproject_url = _required("OPENPROJECT_BASE_URL")
    openproject_token = _required("OPENPROJECT_API_TOKEN")
    store = PostgresLifecycleStore(database_url)
    results: list[dict[str, object]] = []
    trace_id = str(uuid5(NAMESPACE_URL, f"planning-alert-group:{envelope.group_key}"))
    try:
        delivery_job_id = UUID(_required("WM_JOB_ID"))
    except ValueError as error:
        raise RuntimeError("WM_JOB_ID must be a UUID") from error
    adapter = discover_openproject_adapter(
        base_url=openproject_url,
        canonical_origin=_required("OPENPROJECT_CANONICAL_ORIGIN"),
        project_identifier=_required("OPENPROJECT_PROJECT_IDENTIFIER"),
        token=openproject_token,
    )
    with adapter:
        for alert_index, alert in enumerate(envelope.alerts):
            lock_name = f"planning-alert:{alert.fingerprint}"
            payload_sha256 = operational_alert_payload_sha256(alert)
            with psycopg.connect(database_url) as connection, connection.transaction():
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 1176))",
                    (lock_name,),
                )
                transition_at = operational_alert_transition_at(alert)
                current = connection.execute(
                    """
                    SELECT details->>'state',
                           (details->>'transition_at')::timestamptz,
                           (details->>'work_package_id')::bigint
                    FROM planning_lifecycle.audit
                    WHERE action = 'alert_delivery'
                      AND outcome IN ('created', 'updated', 'unchanged')
                      AND details->>'fingerprint' = %s
                      AND details ? 'transition_at'
                      AND details ? 'work_package_id'
                    ORDER BY (details->>'transition_at')::timestamptz DESC,
                             CASE details->>'state'
                               WHEN 'resolved' THEN 1
                               ELSE 0
                             END DESC,
                             occurred_at DESC
                    LIMIT 1
                    """,
                    (alert.fingerprint,),
                ).fetchone()
                event_id = str(
                    uuid5(
                        delivery_job_id,
                        f"{alert_index}:{alert.fingerprint}:{payload_sha256}",
                    )
                )
                if current is not None and operational_alert_is_stale(
                    alert,
                    current_state=str(current[0]),
                    current_transition_at=current[1],
                ):
                    store.audit(
                        event_id=event_id,
                        trace_id=trace_id,
                        action="alert_delivery",
                        outcome="stale",
                        details={
                            "fingerprint": alert.fingerprint,
                            "alertname": alert.name,
                            "state": alert.state,
                            "severity": alert.severity,
                            "payload_sha256": payload_sha256,
                            "transition_at": transition_at.isoformat(),
                            "work_package_id": int(current[2]),
                        },
                        connection=connection,
                    )
                    results.append(
                        {
                            "fingerprint": alert.fingerprint,
                            "state": alert.state,
                            "outcome": "stale",
                            "work_package_id": int(current[2]),
                        }
                    )
                    continue
                effect = adapter.ensure_operational_alert(alert)
                if effect.work_package_id is None:
                    raise RuntimeError("operational alert effect has no work-package ID")
                store.audit(
                    event_id=event_id,
                    trace_id=trace_id,
                    action="alert_delivery",
                    outcome=effect.outcome,
                    details={
                        "fingerprint": alert.fingerprint,
                        "alertname": alert.name,
                        "state": alert.state,
                        "severity": alert.severity,
                        "payload_sha256": payload_sha256,
                        "transition_at": transition_at.isoformat(),
                        "work_package_id": effect.work_package_id,
                    },
                    connection=connection,
                )
                results.append(
                    {
                        "fingerprint": alert.fingerprint,
                        "state": alert.state,
                        "outcome": effect.outcome,
                        "work_package_id": effect.work_package_id,
                    }
                )
    return {
        "receiver": envelope.receiver,
        "delivered": len(results),
        "results": results,
    }
