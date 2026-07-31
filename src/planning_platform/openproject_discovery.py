"""Fail-closed discovery of instance-local OpenProject publication IDs.

OpenProject allocates type, status, priority, project, and custom-field IDs at
database bootstrap time.  Keeping those values in Git would make a clean
restore depend on one historical sequence.  This module resolves the exact
v17.6 API resources by their bootstrapped semantic names and rejects missing,
duplicate, or ambiguous matches.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from .openproject_adapter import OpenProjectAdapterConfig

_CUSTOM_FIELD = re.compile(r"^customField([1-9][0-9]*)$")
_TYPE_NAMES = (
    "Idea",
    "Initiative",
    "Epic",
    "Story",
    "Task",
    "Decision",
    "Investigation",
    "Bug",
)
_STATUS_NAMES = (
    "Draft",
    "Planning",
    "Needs Input",
    "Proposed",
    "Ready",
    "In Progress",
    "Blocked",
    "Review",
    "Done",
    "Superseded",
    "Rejected",
)
_CUSTOM_FIELD_NAMES = {
    "Plan ID": "plan_id",
    "Node key": "node_key",
    "Plan version": "plan_version",
    "Managed hash": "managed_hash",
    "Repository": "repository",
    "Risk": "risk",
    "Agent eligible": "agent_eligible",
    "Source requirements": "source_requirements",
    "Planning commit": "planning_commit",
    "Evidence state": "evidence_state",
    "Alert fingerprint": "alert_fingerprint",
}
_PRIORITY_ALIASES = {
    "low": frozenset({"low"}),
    "medium": frozenset({"normal", "medium"}),
    "high": frozenset({"high"}),
    "critical": frozenset({"immediate", "critical"}),
}


class OpenProjectDiscoveryError(RuntimeError):
    """The OpenProject instance does not match the bootstrapped contract."""


def _document(response: httpx.Response, label: str) -> Mapping[str, Any]:
    if response.status_code != 200:
        raise OpenProjectDiscoveryError(
            f"OpenProject {label} discovery returned {response.status_code}"
        )
    try:
        value = response.json()
    except ValueError as error:
        raise OpenProjectDiscoveryError(f"OpenProject {label} discovery is not JSON") from error
    if not isinstance(value, Mapping):
        raise OpenProjectDiscoveryError(f"OpenProject {label} discovery is not an object")
    return value


def _elements(document: Mapping[str, Any], label: str) -> tuple[Mapping[str, Any], ...]:
    embedded = document.get("_embedded")
    elements = embedded.get("elements") if isinstance(embedded, Mapping) else None
    if not isinstance(elements, Sequence) or isinstance(elements, (str, bytes)):
        raise OpenProjectDiscoveryError(f"OpenProject {label} collection is malformed")
    if not all(isinstance(value, Mapping) for value in elements):
        raise OpenProjectDiscoveryError(f"OpenProject {label} collection has invalid elements")
    count = document.get("count")
    total = document.get("total")
    if type(count) is not int or type(total) is not int or count != len(elements) or total != count:
        raise OpenProjectDiscoveryError(
            f"OpenProject {label} collection must be complete and unpaginated"
        )
    return tuple(value for value in elements if isinstance(value, Mapping))


def _named_ids(
    values: Sequence[Mapping[str, Any]], names: Sequence[str], label: str
) -> dict[str, int]:
    result: dict[str, int] = {}
    for expected in names:
        matches = [
            value
            for value in values
            if isinstance(value.get("name"), str)
            and str(value["name"]).casefold() == expected.casefold()
        ]
        if len(matches) != 1 or type(matches[0].get("id")) is not int:
            raise OpenProjectDiscoveryError(
                f"OpenProject {label} {expected!r} is missing, duplicated, or invalid"
            )
        identifier = int(matches[0]["id"])
        if identifier <= 0 or identifier in result.values():
            raise OpenProjectDiscoveryError(f"OpenProject {label} IDs are invalid or reused")
        result[expected] = identifier
    return result


def _priority_ids(values: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for semantic, aliases in _PRIORITY_ALIASES.items():
        matches = [
            value
            for value in values
            if isinstance(value.get("name"), str)
            and str(value["name"]).casefold() in aliases
            and value.get("isActive") is True
        ]
        if len(matches) != 1 or type(matches[0].get("id")) is not int:
            raise OpenProjectDiscoveryError(
                f"OpenProject active priority for {semantic!r} is missing or ambiguous"
            )
        identifier = int(matches[0]["id"])
        if identifier <= 0 or identifier in result.values():
            raise OpenProjectDiscoveryError("OpenProject priority IDs are invalid or reused")
        result[semantic] = identifier
    return result


def _custom_field_ids(schema: Mapping[str, Any]) -> dict[str, int]:
    matches: dict[str, list[int]] = {name: [] for name in _CUSTOM_FIELD_NAMES}
    for property_name, value in schema.items():
        field = _CUSTOM_FIELD.fullmatch(str(property_name))
        if field is None or not isinstance(value, Mapping):
            continue
        display_name = value.get("name")
        if not isinstance(display_name, str):
            continue
        canonical = next(
            (
                expected
                for expected in _CUSTOM_FIELD_NAMES
                if expected.casefold() == display_name.casefold()
            ),
            None,
        )
        if canonical is not None:
            matches[canonical].append(int(field.group(1)))
    result: dict[str, int] = {}
    for display_name, semantic in _CUSTOM_FIELD_NAMES.items():
        identifiers = matches[display_name]
        if len(identifiers) != 1:
            raise OpenProjectDiscoveryError(
                f"OpenProject custom field {display_name!r} is missing or duplicated"
            )
        result[semantic] = identifiers[0]
    if len(set(result.values())) != len(result):
        raise OpenProjectDiscoveryError("OpenProject custom-field IDs are reused")
    return result


def discover_openproject_config(
    *,
    base_url: str,
    project_identifier: str,
    token: str,
    client: httpx.Client | None = None,
    timeout_seconds: float = 10.0,
    page_size: int = 100,
    max_collection_pages: int = 100,
    max_collection_items: int = 10_000,
) -> OpenProjectAdapterConfig:
    """Resolve a v17.6 publication configuration from API-visible names."""
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("OpenProject base URL must be HTTP(S)")
    if not project_identifier or not token:
        raise ValueError("OpenProject project identifier and API token are required")
    owns_client = client is None
    session = client or httpx.Client(
        base_url=base_url.rstrip("/"),
        auth=httpx.BasicAuth("apikey", token),
        timeout=httpx.Timeout(timeout_seconds),
        headers={"Accept": "application/hal+json"},
    )
    try:
        project_filter = json.dumps(
            [
                {
                    "name_and_identifier": {
                        "operator": "=",
                        "values": [project_identifier],
                    }
                }
            ],
            separators=(",", ":"),
        )
        projects = _elements(
            _document(
                session.get(
                    "/api/v3/projects",
                    params={"filters": project_filter, "pageSize": "2"},
                ),
                "project",
            ),
            "project",
        )
        exact_projects = [
            value
            for value in projects
            if isinstance(value.get("identifier"), str)
            and str(value["identifier"]).casefold() == project_identifier.casefold()
        ]
        if len(exact_projects) != 1 or type(exact_projects[0].get("id")) is not int:
            raise OpenProjectDiscoveryError(
                "OpenProject planning project is missing, duplicated, or invalid"
            )
        project_id = int(exact_projects[0]["id"])
        if project_id <= 0:
            raise OpenProjectDiscoveryError("OpenProject planning project ID is invalid")

        types = _named_ids(
            _elements(
                _document(session.get(f"/api/v3/projects/{project_id}/types"), "types"),
                "types",
            ),
            _TYPE_NAMES,
            "type",
        )
        statuses = _named_ids(
            _elements(_document(session.get("/api/v3/statuses"), "statuses"), "statuses"),
            _STATUS_NAMES,
            "status",
        )
        priorities = _priority_ids(
            _elements(
                _document(session.get("/api/v3/priorities"), "priorities"),
                "priorities",
            )
        )
        idea_schema = _document(
            session.get(f"/api/v3/work_packages/schemas/{project_id}-{types['Idea']}"),
            "work-package schema",
        )
        custom_fields = _custom_field_ids(idea_schema)
        assignee_filters = json.dumps(
            [{"type": {"operator": "=", "values": ["User"]}}],
            separators=(",", ":"),
        )
        assignees = _elements(
            _document(
                session.get(
                    f"/api/v3/workspaces/{project_id}/available_assignees",
                    params={"filters": assignee_filters},
                ),
                "alert assignee",
            ),
            "alert assignee",
        )
        if (
            len(assignees) != 1
            or assignees[0].get("_type") != "User"
            or type(assignees[0].get("id")) is not int
            or int(assignees[0]["id"]) <= 0
        ):
            raise OpenProjectDiscoveryError(
                "OpenProject human alert assignee is missing or ambiguous"
            )
        return OpenProjectAdapterConfig(
            base_url=base_url,
            project_id=project_id,
            type_ids=types,
            status_ids=statuses,
            custom_field_ids=custom_fields,
            alert_assignee_id=int(assignees[0]["id"]),
            priority_ids=priorities,
            timeout_seconds=timeout_seconds,
            page_size=page_size,
            max_collection_pages=max_collection_pages,
            max_collection_items=max_collection_items,
        )
    finally:
        if owns_client:
            session.close()
