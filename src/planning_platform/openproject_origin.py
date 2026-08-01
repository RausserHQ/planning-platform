"""Validation and normalization for OpenProject's public HTTPS origin."""

from __future__ import annotations

import re

import httpx

_ORIGIN = re.compile(
    r"https://(?P<authority>(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?)/?",
    re.IGNORECASE,
)


def canonical_openproject_origin(value: str) -> str:
    """Return one normalized HTTPS origin, rejecting credentials and URL suffixes."""
    if _ORIGIN.fullmatch(value) is None:
        raise ValueError("OpenProject canonical origin must be an HTTPS origin")
    try:
        origin = httpx.URL(value)
    except (httpx.InvalidURL, TypeError) as error:
        raise ValueError("OpenProject canonical origin must be an HTTPS origin") from error
    if (
        origin.scheme != "https"
        or not origin.host
        or origin.userinfo
        or origin.path != "/"
        or origin.query
        or origin.fragment
        or (origin.port is not None and not 1 <= origin.port <= 65_535)
    ):
        raise ValueError("OpenProject canonical origin must be an HTTPS origin")
    try:
        authority = origin.netloc.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("OpenProject canonical origin host must be ASCII") from error
    return f"https://{authority}"
