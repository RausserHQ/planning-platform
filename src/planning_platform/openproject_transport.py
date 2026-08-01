"""Production HTTP transport for an internally addressed OpenProject instance."""

from __future__ import annotations

import httpx

from .openproject_adapter import OpenProjectPublicationAdapter
from .openproject_discovery import discover_openproject_config
from .openproject_origin import canonical_openproject_origin


def _canonical_origin_headers(canonical_origin: str) -> dict[str, str]:
    """Derive reverse-proxy origin headers without accepting a URL path or credentials."""
    normalized = canonical_openproject_origin(canonical_origin)
    host = httpx.URL(normalized).netloc.decode("ascii")
    return {
        "Accept": "application/hal+json",
        "Host": host,
        "X-Forwarded-Proto": "https",
    }


def openproject_client(
    *,
    base_url: str,
    canonical_origin: str,
    token: str,
    timeout_seconds: float = 10.0,
) -> httpx.Client:
    """Build one authenticated client using internal routing and the public origin."""
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("OpenProject base URL must be HTTP(S)")
    if not token:
        raise ValueError("OpenProject API token is required")
    if timeout_seconds <= 0:
        raise ValueError("OpenProject timeout must be positive")
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        auth=httpx.BasicAuth("apikey", token),
        timeout=httpx.Timeout(timeout_seconds),
        headers=_canonical_origin_headers(canonical_origin),
    )


def discover_openproject_adapter(
    *,
    base_url: str,
    canonical_origin: str,
    project_identifier: str,
    token: str,
    timeout_seconds: float = 10.0,
) -> OpenProjectPublicationAdapter:
    """Discover instance-local IDs and return an adapter that owns the shared client."""
    client = openproject_client(
        base_url=base_url,
        canonical_origin=canonical_origin,
        token=token,
        timeout_seconds=timeout_seconds,
    )
    try:
        config = discover_openproject_config(
            base_url=base_url,
            canonical_origin=canonical_origin,
            project_identifier=project_identifier,
            token=token,
            client=client,
            timeout_seconds=timeout_seconds,
        )
        return OpenProjectPublicationAdapter(config, token, client=client)
    except Exception:
        client.close()
        raise
