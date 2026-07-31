"""Authenticated Alertmanager delivery into human-visible OpenProject work packages."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .openproject_adapter import OperationalAlert

_FINGERPRINT = re.compile(r"^[0-9a-f]{8,64}$")
_LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_./-]{0,126}$")
_MAX_BODY_BYTES = 1_048_576
_MAX_ALERTS = 100


class AlertDeliveryRejected(ValueError):
    """The Alertmanager request is unauthenticated, malformed, or out of scope."""


@dataclass(frozen=True)
class AlertmanagerEnvelope:
    receiver: str
    group_key: str
    alerts: tuple[OperationalAlert, ...]


def operational_alert_payload_sha256(alert: OperationalAlert) -> str:
    """Hash every normalized field that can affect an OpenProject alert mutation."""
    encoded = json.dumps(
        {
            "description": alert.description,
            "ends_at": alert.ends_at,
            "fingerprint": alert.fingerprint,
            "labels": dict(sorted(alert.labels.items())),
            "name": alert.name,
            "namespace": alert.namespace,
            "runbook_url": alert.runbook_url,
            "severity": alert.severity,
            "starts_at": alert.starts_at,
            "state": alert.state,
            "summary": alert.summary,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bounded_text(
    value: object,
    label: str,
    *,
    required: bool,
    maximum: int,
) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise AlertDeliveryRejected(f"{label} must be a string")
    text = value.strip()
    if (required and not text) or len(text.encode("utf-8")) > maximum:
        raise AlertDeliveryRejected(f"{label} is empty or too large")
    if "\x00" in text or "planning-platform:generated" in text:
        raise AlertDeliveryRejected(f"{label} contains forbidden content")
    return text


def _timestamp(value: object, label: str, *, required: bool) -> str | None:
    text = _bounded_text(value, label, required=required, maximum=64)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise AlertDeliveryRejected(f"{label} is not an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise AlertDeliveryRejected(f"{label} lacks a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_url(value: object, label: str) -> str | None:
    text = _bounded_text(value, label, required=False, maximum=2048)
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username is not None:
        raise AlertDeliveryRejected(f"{label} is not a safe HTTP(S) URL")
    return text


def _string_map(
    value: object,
    label: str,
    *,
    maximum_items: int,
) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > maximum_items:
        raise AlertDeliveryRejected(f"{label} must be a bounded object")
    result: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or _LABEL_NAME.fullmatch(key) is None:
            raise AlertDeliveryRejected(f"{label} contains an invalid key")
        text = _bounded_text(raw, f"{label}.{key}", required=False, maximum=1024)
        result[key] = text
    return result


def _headers(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > 100:
        raise AlertDeliveryRejected("Windmill trigger headers are absent or unbounded")
    result: dict[str, str] = {}
    for key, raw in value.items():
        if isinstance(key, str) and isinstance(raw, (str, int, float, bool)):
            result[key.casefold()] = str(raw)
    return result


def _parse_alert(value: object) -> OperationalAlert:
    if not isinstance(value, Mapping):
        raise AlertDeliveryRejected("Alertmanager alert is not an object")
    fingerprint = _bounded_text(
        value.get("fingerprint"),
        "alert fingerprint",
        required=True,
        maximum=64,
    ).casefold()
    if _FINGERPRINT.fullmatch(fingerprint) is None:
        raise AlertDeliveryRejected("alert fingerprint is invalid")
    state = _bounded_text(
        value.get("status"),
        "alert status",
        required=True,
        maximum=16,
    ).casefold()
    if state not in {"firing", "resolved"}:
        raise AlertDeliveryRejected("alert status is unsupported")
    labels = _string_map(value.get("labels"), "alert labels", maximum_items=100)
    if labels.get("area") != "planning-platform":
        raise AlertDeliveryRejected("alert is outside the planning-platform route")
    name = _bounded_text(
        labels.get("alertname"),
        "alert name",
        required=True,
        maximum=160,
    )
    severity = _bounded_text(
        labels.get("severity"),
        "alert severity",
        required=True,
        maximum=16,
    ).casefold()
    if severity not in {"warning", "critical"}:
        raise AlertDeliveryRejected("alert severity is unsupported")
    annotations = _string_map(
        value.get("annotations", {}),
        "alert annotations",
        maximum_items=100,
    )
    summary = _bounded_text(
        annotations.get("summary") or name,
        "alert summary",
        required=True,
        maximum=4096,
    )
    description = _bounded_text(
        annotations.get("description"),
        "alert description",
        required=False,
        maximum=16_384,
    )
    starts_at = _timestamp(value.get("startsAt"), "alert startsAt", required=True)
    assert starts_at is not None
    parsed_ends_at = _timestamp(value.get("endsAt"), "alert endsAt", required=False)
    if state == "firing":
        # Alertmanager commonly sends the Go zero-time sentinel while an alert
        # is active. It is not an observed end and must never be rendered as one.
        ends_at = None
    else:
        if parsed_ends_at is None:
            raise AlertDeliveryRejected("resolved alert has no endsAt")
        if datetime.fromisoformat(parsed_ends_at.replace("Z", "+00:00")) < datetime.fromisoformat(
            starts_at.replace("Z", "+00:00")
        ):
            raise AlertDeliveryRejected("resolved alert ends before it starts")
        ends_at = parsed_ends_at
    runbook = _optional_url(annotations.get("runbook_url"), "alert runbook URL")
    return OperationalAlert(
        fingerprint=fingerprint,
        name=name,
        severity=severity,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        summary=summary,
        description=description,
        namespace=labels.get("namespace", ""),
        starts_at=starts_at,
        ends_at=ends_at,
        runbook_url=runbook,
        labels=labels,
    )


def operational_alert_transition_at(alert: OperationalAlert) -> datetime:
    """Return the ordering timestamp for one firing or resolved transition."""
    value = alert.ends_at if alert.state == "resolved" else alert.starts_at
    if value is None:
        raise ValueError("resolved operational alert must have an end timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("operational alert transition timestamp lacks a timezone")
    return parsed.astimezone(UTC)


def operational_alert_is_stale(
    alert: OperationalAlert,
    *,
    current_state: str,
    current_transition_at: datetime,
) -> bool:
    """Reject older transitions; resolution wins a same-instant race."""
    if current_state not in {"firing", "resolved"}:
        raise ValueError("stored operational alert state is invalid")
    if current_transition_at.tzinfo is None:
        raise ValueError("stored operational alert transition lacks a timezone")
    incoming = operational_alert_transition_at(alert)
    current = current_transition_at.astimezone(UTC)
    return incoming < current or (
        incoming == current
        and current_state == "resolved"
        and alert.state == "firing"
    )


def alertmanager_envelope(
    event: Mapping[str, Any],
    *,
    token: str,
) -> AlertmanagerEnvelope:
    """Authenticate the exact raw Windmill v2 request and return bounded alerts."""
    if len(token) < 32:
        raise ValueError("Alertmanager webhook token must contain at least 32 characters")
    raw = event.get("raw_string")
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > _MAX_BODY_BYTES:
        raise AlertDeliveryRejected("Windmill trigger raw body is absent or too large")
    headers = _headers(event.get("headers"))
    supplied = headers.get("authorization", "")
    if not hmac.compare_digest(supplied, f"Bearer {token}"):
        raise AlertDeliveryRejected("Alertmanager authorization is invalid")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AlertDeliveryRejected("Alertmanager body is not JSON") from error
    if not isinstance(payload, Mapping):
        raise AlertDeliveryRejected("Alertmanager body is not an object")
    parsed_body = event.get("body")
    if parsed_body is not None and parsed_body != payload:
        raise AlertDeliveryRejected("Windmill parsed body differs from the raw body")
    receiver = _bounded_text(
        payload.get("receiver"),
        "Alertmanager receiver",
        required=True,
        maximum=255,
    )
    group_key = _bounded_text(
        payload.get("groupKey"),
        "Alertmanager group key",
        required=True,
        maximum=1024,
    )
    values = payload.get("alerts")
    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
        or not 1 <= len(values) <= _MAX_ALERTS
    ):
        raise AlertDeliveryRejected("Alertmanager alerts must be a bounded non-empty list")
    alerts = tuple(_parse_alert(value) for value in values)
    if len({alert.fingerprint for alert in alerts}) != len(alerts):
        raise AlertDeliveryRejected("Alertmanager body contains duplicate fingerprints")
    return AlertmanagerEnvelope(receiver=receiver, group_key=group_key, alerts=alerts)
