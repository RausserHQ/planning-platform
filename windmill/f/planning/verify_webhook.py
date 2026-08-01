"""Verify a Windmill v2 HTTP trigger before any lifecycle effect."""

from __future__ import annotations

import os
from typing import Any, Literal

import httpx

from planning_platform.lifecycle.ingress import (
    github_envelope,
    openproject_envelope,
)
from planning_platform.openproject_transport import openproject_client


def _secret(name: str) -> bytes:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value.encode("utf-8")


def _openproject_service_actor_id() -> str:
    base_url = os.environ.get("OPENPROJECT_BASE_URL", "").rstrip("/")
    canonical_origin = os.environ.get("OPENPROJECT_CANONICAL_ORIGIN", "")
    token = os.environ.get("OPENPROJECT_API_TOKEN", "")
    if not base_url or not canonical_origin or not token:
        raise RuntimeError(
            "OPENPROJECT_BASE_URL, OPENPROJECT_CANONICAL_ORIGIN, and "
            "OPENPROJECT_API_TOKEN are required"
        )
    try:
        with openproject_client(
            base_url=base_url,
            canonical_origin=canonical_origin,
            token=token,
        ) as client:
            response = client.get("/api/v3/users/me")
    except httpx.TransportError as error:
        raise RuntimeError("OpenProject service-identity lookup failed") from error
    if response.status_code != 200:
        raise RuntimeError(
            f"OpenProject service-identity lookup returned {response.status_code}"
        )
    try:
        value = response.json()
    except ValueError as error:
        raise RuntimeError("OpenProject service identity is not JSON") from error
    actor_id = value.get("id") if isinstance(value, dict) else None
    if type(actor_id) is not int or actor_id <= 0:
        raise RuntimeError("OpenProject service identity has no positive ID")
    return str(actor_id)


def main(
    source: Literal["github", "openproject"],
    event: dict[str, Any],
) -> dict[str, Any]:
    if source == "github":
        envelope = github_envelope(
            event,
            secret=_secret("GITHUB_WEBHOOK_SECRET"),
        )
    elif source == "openproject":
        envelope = openproject_envelope(
            event,
            secret=_secret("OPENPROJECT_WEBHOOK_SECRET"),
            trusted_service_actor_ids={_openproject_service_actor_id()},
        )
    else:
        raise ValueError("unsupported webhook source")
    return envelope.model_dump(mode="json")
