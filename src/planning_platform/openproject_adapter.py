# ruff: noqa: E501
"""Fail-closed OpenProject 17.6.0 HTTP publication adapter.

This module is deliberately the only place where planning operation vocabulary
is projected to OpenProject's v3 work-package and relation API.  It accepts no
secret-bearing payloads from plans and never includes its token in exceptions.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import httpx

from .diff import PublicationOperation
from .openproject import (
    OpenProjectSnapshot,
    WorkPackageSnapshot,
    canonical_hash,
    replace_generated_description,
)
from .publication_journal import AmbiguousPublicationEffect

_API_PREFIX = "/api/v3"
_RELATION_PREFIX = "<!-- planning-platform:relation:v1:"
_RELATION_SUFFIX = " -->"
_UPDATE_CONFLICT = "urn:openproject-org:api:v3:errors:UpdateConflict"
_CUSTOM_FIELD_NAMES = frozenset(
    {
        "plan_id",
        "node_key",
        "plan_version",
        "managed_hash",
        "repository",
        "risk",
        "agent_eligible",
        "source_requirements",
        "planning_commit",
        "evidence_state",
        "alert_fingerprint",
    }
)


class OpenProjectPublicationError(RuntimeError):
    """An unsafe response or state transition from OpenProject."""


class OpenProjectConflict(OpenProjectPublicationError):
    """A work package was changed by someone while publication was in flight."""


@dataclass(frozen=True)
class OpenProjectAdapterConfig:
    """Instance-specific IDs, intentionally never embedded in adapter code."""

    base_url: str
    project_id: int
    type_ids: Mapping[str, int]
    status_ids: Mapping[str, int]
    custom_field_ids: Mapping[str, int]
    alert_assignee_id: int
    priority_ids: Mapping[str, int] = field(default_factory=dict)
    timeout_seconds: float = 10.0
    page_size: int = 100
    max_collection_pages: int = 100
    max_collection_items: int = 10_000

    def __post_init__(self) -> None:
        for name in ("type_ids", "status_ids", "custom_field_ids", "priority_ids"):
            values = getattr(self, name)
            if not isinstance(values, Mapping):
                raise ValueError(f"{name} must be a mapping")
            object.__setattr__(self, name, MappingProxyType(dict(values)))
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an HTTP(S) URL")
        if (
            type(self.project_id) is not int
            or self.project_id <= 0
            or type(self.alert_assignee_id) is not int
            or self.alert_assignee_id <= 0
            or self.timeout_seconds <= 0
            or type(self.page_size) is not int
            or not 1 <= self.page_size <= 1000
            or type(self.max_collection_pages) is not int
            or self.max_collection_pages <= 0
            or type(self.max_collection_items) is not int
            or self.max_collection_items < self.page_size
        ):
            raise ValueError("project, timeout, page, and collection bounds must be positive")
        required_types = {
            "Idea",
            "Initiative",
            "Epic",
            "Story",
            "Task",
            "Decision",
            "Investigation",
            "Bug",
        }
        required_statuses = {
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
        }
        if set(self.type_ids) != required_types:
            raise ValueError(
                "type_ids must configure every supported work-package type exactly once"
            )
        if set(self.status_ids) != required_statuses:
            raise ValueError("status_ids must configure every required status exactly once")
        if set(self.custom_field_ids) != _CUSTOM_FIELD_NAMES:
            raise ValueError(
                "custom_field_ids must configure every managed custom field exactly once"
            )
        if set(self.priority_ids) != {"low", "medium", "high", "critical"}:
            raise ValueError(
                "priority_ids must configure low, medium, high, and critical exactly once"
            )
        for name, values in (
            ("type_ids", self.type_ids),
            ("status_ids", self.status_ids),
            ("custom_field_ids", self.custom_field_ids),
            ("priority_ids", self.priority_ids),
        ):
            if any(type(value) is not int or value <= 0 for value in values.values()) or len(
                set(values.values())
            ) != len(values):
                raise ValueError(f"{name} values must be positive numeric IDs")


def openproject_target_sha256(config: OpenProjectAdapterConfig) -> str:
    """Bind durable publication replay to one exact non-secret target config."""
    document = {
        "base_url": config.base_url.rstrip("/"),
        "project_id": config.project_id,
        "alert_assignee_id": config.alert_assignee_id,
        "type_ids": dict(config.type_ids),
        "status_ids": dict(config.status_ids),
        "custom_field_ids": dict(config.custom_field_ids),
        "priority_ids": dict(config.priority_ids),
        "timeout_seconds": float(config.timeout_seconds),
        "page_size": config.page_size,
        "max_collection_pages": config.max_collection_pages,
        "max_collection_items": config.max_collection_items,
    }
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class PublicationEffect:
    operation_id: str
    kind: str
    identity: tuple[str, str]
    outcome: str
    work_package_id: int | None = None


@dataclass(frozen=True)
class OperationalAlert:
    """Bounded Alertmanager state projected into one human-visible work package."""

    fingerprint: str
    name: str
    severity: Literal["warning", "critical"]
    state: Literal["firing", "resolved"]
    summary: str
    description: str
    namespace: str
    starts_at: str
    ends_at: str | None
    runbook_url: str | None
    labels: Mapping[str, str]


@dataclass(frozen=True)
class _ResolvedPackage:
    snapshot: WorkPackageSnapshot
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class _Relation:
    id: int
    from_id: int
    to_id: int
    relation_type: str
    description: str


class OpenProjectPublicationAdapter:
    """Synchronous v3 adapter suitable for the deterministic publisher seam."""

    def __init__(
        self,
        config: OpenProjectAdapterConfig,
        token: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        if not token:
            raise ValueError("OpenProject API token is required")
        self.config = config
        self.publication_target_sha256 = openproject_target_sha256(config)
        self._client = client or httpx.Client(
            base_url=config.base_url.rstrip("/"),
            auth=httpx.BasicAuth("apikey", token),
            timeout=httpx.Timeout(config.timeout_seconds),
            headers={"Accept": "application/hal+json"},
        )
        self.effects: list[PublicationEffect] = []

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenProjectPublicationAdapter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return path

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make one bounded request, withholding response text from exceptions."""
        try:
            response = self._client.request(method, self._url(path), **kwargs)
        except httpx.TransportError as error:
            raise OpenProjectPublicationError("OpenProject transport failure") from error
        if response.status_code != 200:
            raise OpenProjectPublicationError(
                f"OpenProject {method} {path} returned {response.status_code}"
            )
        return response

    def _safe_api_link(
        self,
        href: object,
        *,
        methods: set[str],
        paths: tuple[str, ...],
        absent_method_is_get: bool = False,
    ) -> tuple[str, str]:
        """Validate a server-supplied action link before following it."""
        if not isinstance(href, Mapping) or not isinstance(href.get("href"), str):
            raise OpenProjectPublicationError("OpenProject form has no valid commit link")
        method_value = href.get("method")
        method = (
            "GET"
            if method_value is None and absent_method_is_get
            else str(method_value or "").upper()
        )
        if method not in methods:
            raise OpenProjectPublicationError("OpenProject form commit method is unsafe")
        base = urlparse(self.config.base_url)
        target = urlparse(urljoin(self.config.base_url.rstrip("/") + "/", str(href["href"])))
        if (target.scheme, target.netloc) != (base.scheme, base.netloc) or not any(
            target.path == path for path in paths
        ):
            raise OpenProjectPublicationError("OpenProject form commit target is unsafe")
        return method, target.path + (f"?{target.query}" if target.query else "")

    @staticmethod
    def _json(response: httpx.Response) -> Mapping[str, Any]:
        try:
            value = response.json()
        except ValueError as error:
            raise OpenProjectPublicationError("OpenProject returned invalid JSON") from error
        if not isinstance(value, Mapping):
            raise OpenProjectPublicationError("OpenProject returned an unexpected JSON document")
        return value

    @staticmethod
    def _id_from_link(value: object) -> int | None:
        if isinstance(value, Mapping):
            value = value.get("href")
        if not isinstance(value, str):
            return None
        path = urlparse(value).path.rstrip("/")
        tail = path.rsplit("/", 1)[-1]
        return int(tail) if tail.isdigit() else None

    @staticmethod
    def _link(value: str, identifier: int) -> dict[str, str]:
        return {"href": f"{_API_PREFIX}/{value}/{identifier}"}

    def _field_name(self, semantic: str) -> str:
        return f"customField{self.config.custom_field_ids[semantic]}"

    @staticmethod
    def _field_value(raw: Mapping[str, Any], field_name: str) -> Any:
        value = raw.get(field_name)
        if isinstance(value, Mapping):
            # OpenProject v17.6 represents a `text` custom field as a
            # Formattable document and accepts only `{raw: ...}` on writes.
            return value.get("raw", value.get("title", value.get("href")))
        links = raw.get("_links")
        if isinstance(links, Mapping):
            link = links.get(field_name)
            if isinstance(link, Mapping):
                return link.get("title", link.get("href"))
        return value

    def _identity_from_raw(self, raw: Mapping[str, Any]) -> tuple[str, str] | None:
        plan_id = self._field_value(raw, self._field_name("plan_id"))
        node_key = self._field_value(raw, self._field_name("node_key"))
        if plan_id is None and node_key is None:
            return None
        if (
            plan_id is None
            or node_key is None
            or not str(plan_id).strip()
            or not str(node_key).strip()
        ):
            raise OpenProjectPublicationError(
                "Plan ID and Node key must both be present on a managed work package"
            )
        return str(plan_id), str(node_key)

    def _generated_region(self, raw: Mapping[str, Any]) -> str:
        description = self._description(raw)
        opening = "<!-- planning-platform:generated -->"
        closing = "<!-- /planning-platform:generated -->"
        if description.count(opening) != 1 or description.count(closing) != 1:
            raise OpenProjectPublicationError("generated description markers are malformed")
        start = description.index(opening)
        end = description.index(closing, start) + len(closing)
        return description[start:end]

    def _configured_name(self, raw: Mapping[str, Any], link: str, values: Mapping[str, int]) -> str:
        links = raw.get("_links")
        identifier = self._id_from_link(links.get(link) if isinstance(links, Mapping) else None)
        matches = [name for name, value in values.items() if value == identifier]
        if len(matches) != 1:
            raise OpenProjectPublicationError(f"unknown configured {link} ID")
        return matches[0]

    def _observed_projection(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        encoded_requirements = self._field_value(raw, self._field_name("source_requirements"))
        eligibility = self._field_value(raw, self._field_name("agent_eligible"))
        version = self._field_value(raw, self._field_name("plan_version"))
        risk = self._field_value(raw, self._field_name("risk"))
        if not isinstance(encoded_requirements, str) or not isinstance(eligibility, bool):
            raise OpenProjectPublicationError("managed custom field shape is invalid")
        try:
            source_requirements = json.loads(encoded_requirements)
        except json.JSONDecodeError as error:
            raise OpenProjectPublicationError(
                "Source requirements custom field is not canonical JSON"
            ) from error
        if not isinstance(source_requirements, list) or not all(
            isinstance(value, str) for value in source_requirements
        ):
            raise OpenProjectPublicationError(
                "Source requirements custom field is not a string sequence"
            )
        if encoded_requirements != json.dumps(
            source_requirements,
            ensure_ascii=False,
            separators=(",", ":"),
        ):
            raise OpenProjectPublicationError(
                "Source requirements custom field is not canonical JSON"
            )
        if not isinstance(version, int) or not isinstance(risk, str):
            raise OpenProjectPublicationError("managed custom field shape is invalid")
        priority = self._configured_name(raw, "priority", self.config.priority_ids)
        if priority != risk:
            raise OpenProjectPublicationError("priority and Risk custom field disagree")
        projection = {
            "title": str(raw.get("subject", "")),
            "work_package_type": self._configured_name(raw, "type", self.config.type_ids),
            "generated_description": self._generated_region(raw),
            "priority": priority,
            "risk": risk,
            "repository": self._field_value(raw, self._field_name("repository")),
            "source_requirements": source_requirements,
            "plan_id": self._field_value(raw, self._field_name("plan_id")),
            "node_key": self._field_value(raw, self._field_name("node_key")),
            "plan_version": version,
            "agent_eligibility": eligibility,
            "planning_commit": self._field_value(raw, self._field_name("planning_commit")),
        }
        if not all(
            isinstance(projection[key], str)
            for key in ("repository", "plan_id", "node_key", "planning_commit")
        ):
            raise OpenProjectPublicationError("managed custom field shape is invalid")
        return projection

    def _work_package(
        self, raw: Mapping[str, Any], relations: tuple[tuple[str, tuple[str, str]], ...] = ()
    ) -> WorkPackageSnapshot:
        try:
            package_id = raw["id"]
            lock_version = raw["lockVersion"]
        except KeyError as error:
            raise OpenProjectPublicationError(
                "work package has no numeric ID or lockVersion"
            ) from error
        if (
            type(package_id) is not int
            or package_id <= 0
            or type(lock_version) is not int
            or lock_version < 0
        ):
            raise OpenProjectPublicationError("work package ID or lockVersion is invalid")
        links = raw.get("_links")
        if not isinstance(links, Mapping):
            links = {}
        parent_id = self._id_from_link(links.get("parent"))
        status_id = self._id_from_link(links.get("status"))
        plan_version = self._field_value(raw, self._field_name("plan_version"))
        repository = self._field_value(raw, self._field_name("repository"))
        identity = self._identity_from_raw(raw)
        stored_hash = self._field_value(raw, self._field_name("managed_hash"))
        evidence_state = self._field_value(raw, self._field_name("evidence_state"))
        if identity is not None:
            if not isinstance(stored_hash, str) or len(stored_hash) != 64:
                raise OpenProjectPublicationError("managed work package has no valid Managed hash")
            if evidence_state is not None and not isinstance(evidence_state, str):
                raise OpenProjectPublicationError("Evidence state custom field is not a string")
            if not isinstance(repository, str) or not repository:
                raise OpenProjectPublicationError("Repository custom field is not a string")
            observed_hash = canonical_hash(self._observed_projection(raw))
            if stored_hash != observed_hash:
                raise OpenProjectPublicationError(
                    "stored Managed hash disagrees with observed managed state"
                )
        try:
            parsed_plan_version = None if plan_version in (None, "") else int(plan_version)
        except (TypeError, ValueError) as error:
            raise OpenProjectPublicationError(
                "Plan version custom field is not an integer"
            ) from error
        return WorkPackageSnapshot(
            id=package_id,
            lock_version=lock_version,
            plan_id=None if identity is None else identity[0],
            node_key=None if identity is None else identity[1],
            plan_version=parsed_plan_version,
            repository=None if identity is None else repository,
            title=str(raw.get("subject", raw.get("title", ""))),
            managed_hash=(
                None
                if self._field_value(raw, self._field_name("managed_hash")) is None
                else str(self._field_value(raw, self._field_name("managed_hash")))
            ),
            parent_id=parent_id,
            relations=(),
            managed_relations=relations,
            human_fields={
                "description": self._description(raw),
                "status_id": status_id,
            },
            superseded=status_id == self.config.status_ids["Superseded"],
            evidence_state=evidence_state,
        )

    @staticmethod
    def _description(raw: Mapping[str, Any]) -> str:
        description = raw.get("description", "")
        if isinstance(description, Mapping):
            description = description.get("raw", "")
        return str(description or "")

    def _page(self, path: str, *, params: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        response = self._request("GET", path, params=params)
        document = self._json(response)
        embedded = document.get("_embedded")
        if not isinstance(embedded, Mapping):
            raise OpenProjectPublicationError("OpenProject collection has no embedded elements")
        elements = embedded.get("elements")
        if not isinstance(elements, list) or not all(
            isinstance(item, Mapping) for item in elements
        ):
            raise OpenProjectPublicationError("OpenProject collection elements are invalid")
        return document

    def _collection(self, path: str, *, params: Mapping[str, str]) -> list[Mapping[str, Any]]:
        """Follow v17.6 offset pages (offset is a one-based page number)."""
        current_path = path
        current_params = dict(params)
        current_params["offset"] = "1"
        values: list[Mapping[str, Any]] = []
        visited: set[tuple[str, str]] = set()
        expected_total: int | None = None
        pages = 0
        while True:
            pages += 1
            if pages > self.config.max_collection_pages:
                raise OpenProjectPublicationError(
                    "OpenProject collection exceeds the configured page bound"
                )
            request_key = (current_path, str(httpx.QueryParams(current_params)))
            if request_key in visited:
                raise OpenProjectPublicationError("OpenProject pagination link loop")
            visited.add(request_key)
            requested_offset = current_params.get("offset")
            if requested_offset is None or not requested_offset.isdigit():
                raise OpenProjectPublicationError(
                    "OpenProject pagination link has an invalid offset"
                )
            response = self._page(current_path, params=current_params)
            embedded = response["_embedded"]
            assert isinstance(embedded, Mapping)
            elements = embedded["elements"]
            assert isinstance(elements, list)
            count = response.get("count")
            total = response.get("total")
            offset = response.get("offset")
            if not all(type(value) is int for value in (count, total, offset)):
                raise OpenProjectPublicationError(
                    "OpenProject collection lacks pagination metadata"
                )
            assert isinstance(count, int) and isinstance(total, int) and isinstance(offset, int)
            if expected_total is None:
                expected_total = total
            if (
                count != len(elements)
                or offset != int(requested_offset)
                or offset < 1
                or total < 0
                or total > self.config.max_collection_items
                or total != expected_total
                or len(values) + count > total
                or (count == 0 and len(values) < total)
            ):
                raise OpenProjectPublicationError(
                    "OpenProject collection pagination metadata is inconsistent"
                )
            values.extend(elements)
            links = response.get("_links")
            next_link = links.get("nextByOffset") if isinstance(links, Mapping) else None
            if len(values) == total:
                if next_link is not None:
                    raise OpenProjectPublicationError(
                        "OpenProject collection pagination metadata is inconsistent"
                    )
                return values
            if next_link is not None:
                _, target = self._safe_api_link(
                    next_link,
                    methods={"GET"},
                    paths=(path,),
                    absent_method_is_get=True,
                )
                next_path, _, query = target.partition("?")
                next_params = dict(httpx.QueryParams(query))
                next_offset = next_params.get("offset")
                if (
                    next_offset is None
                    or not next_offset.isdigit()
                    or int(next_offset) != offset + 1
                    or any(next_params.get(key) != value for key, value in params.items())
                ):
                    raise OpenProjectPublicationError(
                        "OpenProject pagination link does not preserve collection scope"
                    )
                current_path = next_path
                current_params = next_params
                continue
            # A missing next link is tolerated only when metadata proves a next page;
            # request the next *page* (not record offset). The request and response
            # offsets above prove progress and bound the loop by the stable total.
            current_params["offset"] = str(offset + 1)

    def _project_work_packages(self) -> list[Mapping[str, Any]]:
        path = f"{_API_PREFIX}/projects/{self.config.project_id}/work_packages"
        return self._collection(path, params={"pageSize": str(self.config.page_size)})

    def _get_work_package(self, package_id: int) -> Mapping[str, Any]:
        return self._json(self._request("GET", f"{_API_PREFIX}/work_packages/{package_id}"))

    def _identity_matches(self, identity: tuple[str, str]) -> list[_ResolvedPackage]:
        matches: list[_ResolvedPackage] = []
        for raw in self._project_work_packages():
            if self._identity_from_raw(raw) != identity:
                continue
            # The collection is only a discovery list. A write always starts from
            # this individual, current representation and lockVersion.
            current = self._get_work_package(int(raw["id"]))
            if self._identity_from_raw(current) == identity:
                snapshot = self._work_package(current)
                relations = self._managed_relations_for(snapshot.id, identity)
                parent_identity: tuple[str, str] | None = None
                if snapshot.parent_id is not None:
                    parent_identity = self._identity_from_raw(
                        self._get_work_package(snapshot.parent_id)
                    )
                snapshot = replace(
                    snapshot, parent_identity=parent_identity, managed_relations=relations
                )
                matches.append(_ResolvedPackage(snapshot, current))
        if len(matches) > 1:
            raise OpenProjectPublicationError(
                f"duplicate publication identity: {identity[0]}:{identity[1]}"
            )
        return matches

    def resolve(self, identity: tuple[str, str]) -> WorkPackageSnapshot | None:
        matches = self._identity_matches(identity)
        return None if not matches else matches[0].snapshot

    @staticmethod
    def _expected_relations(value: object) -> tuple[tuple[str, tuple[str, str]], ...]:
        if not isinstance(value, list):
            raise OpenProjectConflict("managed relation precondition is malformed")
        result: list[tuple[str, tuple[str, str]]] = []
        for relation in value:
            if (
                not isinstance(relation, (list, tuple))
                or len(relation) != 2
                or not isinstance(relation[0], str)
                or not isinstance(relation[1], (list, tuple))
                or len(relation[1]) != 2
                or not all(isinstance(part, str) for part in relation[1])
            ):
                raise OpenProjectConflict("managed relation precondition is malformed")
            result.append((relation[0], (relation[1][0], relation[1][1])))
        return tuple(sorted(result))

    def _assert_topology_preconditions(
        self, operation: PublicationOperation, current: WorkPackageSnapshot
    ) -> None:
        conditions = operation.preconditions
        required = {
            "expected_parent_identity",
            "expected_unmanaged_parent",
            "expected_managed_relations",
        }
        if not required <= conditions.keys():
            raise OpenProjectConflict("topology preconditions are missing")
        expected_parent = conditions["expected_parent_identity"]
        if isinstance(expected_parent, list):
            expected_parent = tuple(expected_parent)
        if expected_parent is not None and (
            not isinstance(expected_parent, tuple)
            or len(expected_parent) != 2
            or not all(isinstance(part, str) for part in expected_parent)
        ):
            raise OpenProjectConflict("parent precondition is malformed")
        expected_unmanaged = conditions.get("expected_unmanaged_parent")
        if not isinstance(expected_unmanaged, bool):
            raise OpenProjectConflict("parent precondition is malformed")
        if expected_parent is not None and expected_unmanaged:
            raise OpenProjectConflict("parent precondition is contradictory")
        actual_unmanaged = current.parent_id is not None and current.parent_identity is None
        if current.parent_identity != expected_parent or actual_unmanaged != expected_unmanaged:
            raise OpenProjectConflict("parent topology changed before publication")
        expected_relations = self._expected_relations(conditions.get("expected_managed_relations"))
        if tuple(sorted(current.managed_relations)) != expected_relations:
            raise OpenProjectConflict("managed relation topology changed before publication")

    def _postcondition_topology(
        self, operation: PublicationOperation, current: WorkPackageSnapshot
    ) -> bool:
        """Compare the exact topology expected after one deterministic effect."""
        conditions = operation.preconditions
        required = {
            "expected_parent_identity",
            "expected_unmanaged_parent",
            "expected_managed_relations",
        }
        if not required <= conditions.keys():
            raise OpenProjectConflict("topology preconditions are missing")
        expected_parent = conditions["expected_parent_identity"]
        if isinstance(expected_parent, list):
            expected_parent = tuple(expected_parent)
        if expected_parent is not None and (
            not isinstance(expected_parent, tuple)
            or len(expected_parent) != 2
            or not all(isinstance(part, str) for part in expected_parent)
        ):
            raise OpenProjectConflict("parent precondition is malformed")
        expected_unmanaged = conditions["expected_unmanaged_parent"]
        if not isinstance(expected_unmanaged, bool):
            raise OpenProjectConflict("parent precondition is malformed")
        expected_relations = set(self._expected_relations(conditions["expected_managed_relations"]))
        if operation.kind == "set_parent":
            requested_parent = operation.payload.get("parent_identity")
            if isinstance(requested_parent, list):
                requested_parent = tuple(requested_parent)
            if requested_parent is not None and (
                not isinstance(requested_parent, tuple)
                or len(requested_parent) != 2
                or not all(isinstance(part, str) for part in requested_parent)
            ):
                raise OpenProjectConflict("parent postcondition is malformed")
            expected_parent = requested_parent
            expected_unmanaged = False
        elif operation.kind in {"create_relation", "remove_managed_relation"}:
            semantic = operation.payload.get("type")
            target = operation.payload.get("target_identity")
            if isinstance(target, list):
                target = tuple(target)
            if (
                not isinstance(semantic, str)
                or not isinstance(target, tuple)
                or len(target) != 2
                or not all(isinstance(part, str) for part in target)
            ):
                raise OpenProjectConflict("relation postcondition is malformed")
            relation = (semantic, (target[0], target[1]))
            if operation.kind == "create_relation":
                if relation in expected_relations:
                    raise OpenProjectConflict("relation create postcondition is contradictory")
                expected_relations.add(relation)
            else:
                if relation not in expected_relations:
                    raise OpenProjectConflict("relation removal postcondition is contradictory")
                expected_relations.remove(relation)
        if expected_parent is None:
            parent_matches = (
                current.parent_identity is None
                and (current.parent_id is not None) == expected_unmanaged
            )
        else:
            parent_matches = not expected_unmanaged and current.parent_identity == expected_parent
        return parent_matches and tuple(sorted(current.managed_relations)) == tuple(
            sorted(expected_relations)
        )

    def _relations(self, package_id: int) -> list[_Relation]:
        """Read relations through the required global collection endpoint."""
        path = f"{_API_PREFIX}/relations"
        filters = json.dumps(
            [{"involved": {"operator": "=", "values": [str(package_id)]}}],
            separators=(",", ":"),
        )
        found: list[_Relation] = []
        for raw in self._collection(
            path,
            params={"filters": filters, "pageSize": str(self.config.page_size)},
        ):
            links = raw.get("_links")
            if not isinstance(links, Mapping):
                raise OpenProjectPublicationError("relation has no links")
            from_id = self._id_from_link(links.get("from"))
            to_id = self._id_from_link(links.get("to"))
            relation_type = str(raw.get("type", ""))
            if from_id is None or to_id is None or not relation_type:
                raise OpenProjectPublicationError("relation has invalid endpoints or type")
            description = raw.get("description", "")
            if isinstance(description, Mapping):
                description = description.get("raw", "")
            found.append(
                _Relation(int(raw["id"]), from_id, to_id, relation_type, str(description or ""))
            )
        return found

    def _identity_for_package_id(self, package_id: int) -> tuple[str, str] | None:
        """Resolve an endpoint identity without recursing through relations."""
        return self._identity_from_raw(self._get_work_package(package_id))

    @staticmethod
    def _relation_marker(semantic: str, source: tuple[str, str], target: tuple[str, str]) -> str:
        if semantic == "related_to" and target < source:
            source, target = target, source
        encoded = json.dumps(
            {"semantic": semantic, "source": source, "target": target},
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"{_RELATION_PREFIX}{encoded}{_RELATION_SUFFIX}"

    @staticmethod
    def _parse_relation_marker(
        description: str,
    ) -> tuple[str, tuple[str, str], tuple[str, str]] | None:
        if not (
            description.startswith(_RELATION_PREFIX) and description.endswith(_RELATION_SUFFIX)
        ):
            return None
        try:
            value = json.loads(description[len(_RELATION_PREFIX) : -len(_RELATION_SUFFIX)])
            semantic = value["semantic"]
            source = tuple(value["source"])
            target = tuple(value["target"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            semantic
            not in {
                "blocked_by",
                "sequence_after",
                "related_to",
                "decision_required",
                "governed_by",
            }
            or len(source) != 2
            or len(target) != 2
            or not all(isinstance(part, str) for part in (*source, *target))
        ):
            return None
        return semantic, (source[0], source[1]), (target[0], target[1])

    @staticmethod
    def _project_relation(semantic: str, source_id: int, target_id: int) -> tuple[int, int, str]:
        projections = {
            # A planning item `source blocked_by target` is stored in
            # OpenProject as `target blocks source`.
            "blocked_by": (target_id, source_id, "blocks"),
            "sequence_after": (source_id, target_id, "follows"),
            "decision_required": (source_id, target_id, "requires"),
            "governed_by": (source_id, target_id, "relates"),
        }
        if semantic == "related_to":
            from_id, to_id = sorted((source_id, target_id))
            return from_id, to_id, "relates"
        try:
            return projections[semantic]
        except KeyError as error:
            raise OpenProjectPublicationError(f"unknown planning relation: {semantic}") from error

    def _relation_matches_marker(
        self,
        relation: _Relation,
        semantic: str,
        source: tuple[str, str],
        target: tuple[str, str],
    ) -> bool:
        from_identity = self._identity_for_package_id(relation.from_id)
        to_identity = self._identity_for_package_id(relation.to_id)
        if from_identity == source and to_identity == target:
            source_id, target_id = relation.from_id, relation.to_id
        elif from_identity == target and to_identity == source:
            source_id, target_id = relation.to_id, relation.from_id
        else:
            return False
        expected_from, expected_to, native_type = self._project_relation(
            semantic, source_id, target_id
        )
        return (relation.from_id, relation.to_id, relation.relation_type) == (
            expected_from,
            expected_to,
            native_type,
        )

    def _managed_relations_for(
        self, package_id: int, package_identity: tuple[str, str]
    ) -> tuple[tuple[str, tuple[str, str]], ...]:
        result: set[tuple[str, tuple[str, str]]] = set()
        for relation in self._relations(package_id):
            parsed = self._parse_relation_marker(relation.description)
            if parsed is None:
                continue
            semantic, source, target = parsed
            if source == package_identity:
                if not self._relation_matches_marker(relation, semantic, source, target):
                    raise OpenProjectPublicationError(
                        "managed relation marker disagrees with native endpoints or type"
                    )
                result.add((semantic, target))
            elif semantic == "related_to" and target == package_identity:
                if not self._relation_matches_marker(relation, semantic, source, target):
                    raise OpenProjectPublicationError(
                        "managed relation marker disagrees with native endpoints or type"
                    )
                # `related_to` is symmetric natively, but the deterministic
                # plan graph assigns it only to the lower stable-identity
                # endpoint recorded as the marker source. Exposing it on both
                # endpoints makes the higher endpoint remove it on every replay.
        return tuple(sorted(result))

    def snapshot(self) -> OpenProjectSnapshot:
        values = self._project_work_packages()
        packages: list[WorkPackageSnapshot] = []
        etag_parts: list[str] = []
        for raw in values:
            current_response = self._request("GET", f"{_API_PREFIX}/work_packages/{int(raw['id'])}")
            current = self._json(current_response)
            identity = self._identity_from_raw(current)
            relations: tuple[tuple[str, tuple[str, str]], ...] = ()
            if identity is not None:
                relations = self._managed_relations_for(int(current["id"]), identity)
            packages.append(self._work_package(current, relations))
            etag_parts.append(current_response.headers.get("ETag", ""))
        packages_tuple = tuple(sorted(packages, key=lambda package: package.id))
        by_id = {package.id: package for package in packages_tuple}
        # Parent IDs are OpenProject-local; expose the durable managed identity
        # only after every package has been read.
        packages_tuple = tuple(
            replace(
                package,
                parent_identity=(
                    None
                    if package.parent_id is None or by_id.get(package.parent_id) is None
                    else by_id[package.parent_id].identity
                ),
            )
            for package in packages_tuple
        )
        digest = canonical_hash([package.__dict__ for package in packages_tuple])
        return OpenProjectSnapshot(
            captured_at=datetime.now(UTC).isoformat(),
            etag=hashlib.sha256("|".join(etag_parts).encode()).hexdigest(),
            sha256=digest,
            work_packages=packages_tuple,
        )

    def read_work_package(self, package_id: int) -> Mapping[str, Any]:
        """Read one current HAL representation for lifecycle intake."""
        if type(package_id) is not int or package_id <= 0:
            raise ValueError("work package ID must be a positive integer")
        return self._get_work_package(package_id)

    def set_lifecycle_state(
        self,
        package_id: int,
        *,
        status: str | None = None,
        evidence_state: str | None = None,
    ) -> Mapping[str, Any]:
        """Update only lifecycle-owned status/evidence fields through a v3 form."""
        if status is None and evidence_state is None:
            raise ValueError("at least one lifecycle field is required")
        current = self._get_work_package(package_id)
        current_snapshot = self._work_package(current)
        payload: dict[str, Any] = {"lockVersion": current_snapshot.lock_version}
        links: dict[str, Any] = {}
        if status is not None:
            try:
                status_id = self.config.status_ids[status]
            except KeyError as error:
                raise ValueError(f"unknown configured lifecycle status: {status}") from error
            current_status = current_snapshot.human_fields.get("status_id")
            if current_status != status_id:
                links["status"] = self._link("statuses", status_id)
        if evidence_state is not None:
            if not evidence_state or len(evidence_state) > 255:
                raise ValueError("evidence state must be between 1 and 255 characters")
            current_evidence = self._field_value(current, self._field_name("evidence_state"))
            if current_evidence != evidence_state:
                payload[self._field_name("evidence_state")] = evidence_state
        if not links and len(payload) == 1:
            return current
        if links:
            payload["_links"] = links
        validated, method, target = self._validated_form(
            f"{_API_PREFIX}/work_packages/{package_id}/form",
            payload,
            commit_paths=(f"{_API_PREFIX}/work_packages/{package_id}",),
            methods={"PATCH"},
        )
        commit_payload = dict(validated)
        commit_payload["lockVersion"] = current_snapshot.lock_version
        response = self._client.request(method, target, json=commit_payload)
        if response.status_code == 409:
            raise OpenProjectConflict("OpenProject lifecycle update conflicted")
        if response.status_code != 200:
            raise OpenProjectPublicationError(
                f"OpenProject lifecycle update returned {response.status_code}"
            )
        result = self._json(response)
        verified = self._work_package(result)
        if (
            status is not None
            and verified.human_fields.get("status_id") != self.config.status_ids[status]
        ):
            raise OpenProjectPublicationError(
                "OpenProject lifecycle status postcondition is absent"
            )
        if evidence_state is not None and verified.evidence_state != evidence_state:
            raise OpenProjectPublicationError(
                "OpenProject lifecycle evidence postcondition is absent"
            )
        return result

    @staticmethod
    def _operational_alert_subject(alert: OperationalAlert) -> str:
        subject = f"[Planning alert {alert.fingerprint}] {alert.name}"
        if len(subject) > 255:
            raise ValueError("operational alert subject exceeds 255 characters")
        return subject

    @staticmethod
    def _operational_alert_region(alert: OperationalAlert) -> str:
        labels = json.dumps(
            dict(sorted(alert.labels.items())),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        lines = [
            "<!-- planning-platform:generated -->",
            "## Operational alert",
            "",
            f"- State: **{alert.state}**",
            f"- Severity: **{alert.severity}**",
            f"- Fingerprint: `{alert.fingerprint}`",
            f"- Namespace: `{alert.namespace or 'none'}`",
            f"- Started: `{alert.starts_at}`",
        ]
        if alert.ends_at is not None:
            lines.append(f"- Ended: `{alert.ends_at}`")
        lines.extend(["", alert.summary])
        if alert.description:
            lines.extend(["", alert.description])
        if alert.runbook_url is not None:
            lines.extend(["", f"[Runbook]({alert.runbook_url})"])
        lines.extend(["", "### Alert labels", ""])
        lines.extend(f"    {line}" for line in labels.splitlines())
        lines.append("<!-- /planning-platform:generated -->")
        rendered = "\n".join(lines)
        if len(rendered.encode("utf-8")) > 65_536:
            raise ValueError("operational alert description exceeds 65536 bytes")
        return rendered

    def _find_operational_alert(self, fingerprint: str) -> _ResolvedPackage | None:
        matches = [
            raw
            for raw in self._project_work_packages()
            if self._field_value(raw, self._field_name("alert_fingerprint"))
            == fingerprint
        ]
        if len(matches) > 1:
            raise OpenProjectPublicationError(
                "duplicate operational alert work packages exist"
            )
        if not matches:
            marker = f"- Fingerprint: `{fingerprint}`"
            orphaned = [
                raw
                for raw in self._project_work_packages()
                if marker in self._description(raw)
                and "<!-- planning-platform:generated -->" in self._description(raw)
            ]
            if orphaned:
                raise OpenProjectPublicationError(
                    "operational alert identity field was removed or changed"
                )
            return None
        raw = matches[0]
        description = self._description(raw)
        if (
            description.count("<!-- planning-platform:generated -->") != 1
            or description.count("<!-- /planning-platform:generated -->") != 1
            or f"- Fingerprint: `{fingerprint}`" not in description
        ):
            raise OpenProjectPublicationError(
                "operational alert identity collides with non-alert content"
            )
        return _ResolvedPackage(snapshot=self._work_package(raw), raw=raw)

    def _operational_alert_matches(
        self,
        raw: Mapping[str, Any],
        *,
        fingerprint: str,
        description: str,
        status_id: int,
        priority_id: int,
        required_assignee_id: int | None = None,
    ) -> bool:
        links = raw.get("_links")
        return (
            self._field_value(raw, self._field_name("alert_fingerprint"))
            == fingerprint
            and self._description(raw) == description
            and isinstance(links, Mapping)
            and self._id_from_link(links.get("status")) == status_id
            and self._id_from_link(links.get("priority")) == priority_id
            and (
                required_assignee_id is None
                or self._id_from_link(links.get("assignee")) == required_assignee_id
            )
        )

    def ensure_operational_alert(self, alert: OperationalAlert) -> PublicationEffect:
        """Create or converge one Alertmanager fingerprint without touching human text."""
        subject = self._operational_alert_subject(alert)
        generated = self._operational_alert_region(alert)
        status_id = self.config.status_ids[
            "Blocked" if alert.state == "firing" else "Done"
        ]
        priority_id = self.config.priority_ids[
            "critical" if alert.severity == "critical" else "high"
        ]
        identity = ("operational-alert", alert.fingerprint)
        resolved = self._find_operational_alert(alert.fingerprint)
        if resolved is None:
            payload: dict[str, Any] = {
                "subject": subject,
                "description": {"raw": generated},
                self._field_name("alert_fingerprint"): alert.fingerprint,
                "_links": {
                    "project": self._link("projects", self.config.project_id),
                    "type": self._link("types", self.config.type_ids["Task"]),
                    "status": self._link("statuses", status_id),
                    "priority": self._link("priorities", priority_id),
                    "assignee": self._link("users", self.config.alert_assignee_id),
                },
            }
            validated, method, target = self._validated_form(
                f"{_API_PREFIX}/work_packages/form",
                payload,
                commit_paths=(f"{_API_PREFIX}/work_packages",),
                methods={"POST"},
            )
            try:
                response = self._client.request(method, target, json=validated)
            except httpx.TransportError as error:
                recovered = self._find_operational_alert(alert.fingerprint)
                if recovered is not None and self._operational_alert_matches(
                    recovered.raw,
                    fingerprint=alert.fingerprint,
                    description=generated,
                    status_id=status_id,
                    priority_id=priority_id,
                    required_assignee_id=self.config.alert_assignee_id,
                ):
                    return PublicationEffect(
                        operation_id=f"alert:{alert.fingerprint}:{alert.state}",
                        kind="operational_alert",
                        identity=identity,
                        outcome="created",
                        work_package_id=recovered.snapshot.id,
                    )
                raise AmbiguousPublicationEffect(
                    "OpenProject alert create response was lost and postcondition is absent"
                ) from error
            if response.status_code != 201:
                if response.status_code == 408 or response.status_code >= 500:
                    recovered = self._find_operational_alert(alert.fingerprint)
                    if recovered is not None and self._operational_alert_matches(
                        recovered.raw,
                        fingerprint=alert.fingerprint,
                        description=generated,
                        status_id=status_id,
                        priority_id=priority_id,
                        required_assignee_id=self.config.alert_assignee_id,
                    ):
                        return PublicationEffect(
                            operation_id=f"alert:{alert.fingerprint}:{alert.state}",
                            kind="operational_alert",
                            identity=identity,
                            outcome="created",
                            work_package_id=recovered.snapshot.id,
                        )
                    raise AmbiguousPublicationEffect(
                        "OpenProject alert create returned a possibly committed failure"
                    )
                raise OpenProjectPublicationError(
                    f"OpenProject alert create returned {response.status_code}"
                )
            raw = self._json(response)
            created = self._work_package(raw)
            if not self._operational_alert_matches(
                raw,
                fingerprint=alert.fingerprint,
                description=generated,
                status_id=status_id,
                priority_id=priority_id,
                required_assignee_id=self.config.alert_assignee_id,
            ):
                raise OpenProjectPublicationError(
                    "OpenProject alert create postcondition is absent"
                )
            return PublicationEffect(
                operation_id=f"alert:{alert.fingerprint}:{alert.state}",
                kind="operational_alert",
                identity=identity,
                outcome="created",
                work_package_id=created.id,
            )

        for attempt in range(2):
            current = (
                resolved
                if attempt == 0
                else self._find_operational_alert(alert.fingerprint)
            )
            if current is None:
                raise OpenProjectConflict(
                    "operational alert disappeared during update"
                )
            try:
                description = replace_generated_description(
                    self._description(current.raw),
                    generated,
                )
            except ValueError as error:
                raise OpenProjectPublicationError(
                    "operational alert generated markers are malformed"
                ) from error
            if self._operational_alert_matches(
                current.raw,
                fingerprint=alert.fingerprint,
                description=description,
                status_id=status_id,
                priority_id=priority_id,
            ):
                return PublicationEffect(
                    operation_id=f"alert:{alert.fingerprint}:{alert.state}",
                    kind="operational_alert",
                    identity=identity,
                    outcome="unchanged",
                    work_package_id=current.snapshot.id,
                )
            payload = {
                "lockVersion": current.snapshot.lock_version,
                "description": {"raw": description},
                "_links": {
                    "status": self._link("statuses", status_id),
                    "priority": self._link("priorities", priority_id),
                },
            }
            validated, method, target = self._validated_form(
                f"{_API_PREFIX}/work_packages/{current.snapshot.id}/form",
                payload,
                commit_paths=(
                    f"{_API_PREFIX}/work_packages/{current.snapshot.id}",
                ),
                methods={"PATCH"},
            )
            commit_payload = dict(validated)
            commit_payload["lockVersion"] = current.snapshot.lock_version
            try:
                response = self._client.request(method, target, json=commit_payload)
            except httpx.TransportError as error:
                recovered = self._find_operational_alert(alert.fingerprint)
                if recovered is not None and self._operational_alert_matches(
                    recovered.raw,
                    fingerprint=alert.fingerprint,
                    description=description,
                    status_id=status_id,
                    priority_id=priority_id,
                ):
                    return PublicationEffect(
                        operation_id=f"alert:{alert.fingerprint}:{alert.state}",
                        kind="operational_alert",
                        identity=identity,
                        outcome="updated",
                        work_package_id=recovered.snapshot.id,
                    )
                raise AmbiguousPublicationEffect(
                    "OpenProject alert update response was lost and postcondition is absent"
                ) from error
            if response.status_code == 409 and attempt == 0:
                continue
            if response.status_code == 409:
                raise OpenProjectConflict("OpenProject alert update conflicted")
            if response.status_code != 200:
                if response.status_code == 408 or response.status_code >= 500:
                    recovered = self._find_operational_alert(alert.fingerprint)
                    if recovered is not None and self._operational_alert_matches(
                        recovered.raw,
                        fingerprint=alert.fingerprint,
                        description=description,
                        status_id=status_id,
                        priority_id=priority_id,
                    ):
                        return PublicationEffect(
                            operation_id=f"alert:{alert.fingerprint}:{alert.state}",
                            kind="operational_alert",
                            identity=identity,
                            outcome="updated",
                            work_package_id=recovered.snapshot.id,
                        )
                    raise AmbiguousPublicationEffect(
                        "OpenProject alert update returned a possibly committed failure"
                    )
                raise OpenProjectPublicationError(
                    f"OpenProject alert update returned {response.status_code}"
                )
            raw = self._json(response)
            updated = self._work_package(raw)
            if not self._operational_alert_matches(
                raw,
                fingerprint=alert.fingerprint,
                description=description,
                status_id=status_id,
                priority_id=priority_id,
            ):
                raise OpenProjectPublicationError(
                    "OpenProject alert update postcondition is absent"
                )
            return PublicationEffect(
                operation_id=f"alert:{alert.fingerprint}:{alert.state}",
                kind="operational_alert",
                identity=identity,
                outcome="updated",
                work_package_id=updated.id,
            )
        raise OpenProjectConflict("OpenProject alert update did not converge")

    def ensure_comment(self, package_id: int, body: str, *, idempotency_key: str) -> int:
        """Add one marker-bound comment and converge after delivery replay."""
        if not body.strip() or len(body.encode("utf-8")) > 65_536:
            raise ValueError("OpenProject comment must be non-empty and bounded")
        if not re.fullmatch(r"[a-z0-9][a-z0-9:._/-]{7,255}", idempotency_key):
            raise ValueError("OpenProject comment idempotency key is invalid")
        marker = f"<!-- planning-platform:comment:{idempotency_key} -->"
        rendered = f"{body.rstrip()}\n\n{marker}"
        path = f"{_API_PREFIX}/work_packages/{package_id}/activities"

        def existing() -> int | None:
            found: list[int] = []
            for activity in self._collection(path, params={"pageSize": str(self.config.page_size)}):
                comment = activity.get("comment")
                raw = comment.get("raw") if isinstance(comment, Mapping) else None
                if isinstance(raw, str) and marker in raw:
                    identifier = activity.get("id")
                    if type(identifier) is not int or identifier <= 0:
                        raise OpenProjectPublicationError("marker-bound activity has an invalid ID")
                    found.append(identifier)
            if len(found) > 1:
                raise OpenProjectPublicationError(
                    "duplicate marker-bound OpenProject comments exist"
                )
            return found[0] if found else None

        found = existing()
        if found is not None:
            return found
        try:
            response = self._client.post(path, json={"comment": {"raw": rendered}})
        except httpx.TransportError as error:
            recovered = existing()
            if recovered is not None:
                return recovered
            raise AmbiguousPublicationEffect(
                "OpenProject comment response was lost and its marker is absent"
            ) from error
        if response.status_code != 201:
            if response.status_code == 408 or response.status_code >= 500:
                recovered = existing()
                if recovered is not None:
                    return recovered
                raise AmbiguousPublicationEffect(
                    "OpenProject comment returned a possibly committed server failure"
                )
            raise OpenProjectPublicationError(
                f"OpenProject comment returned {response.status_code}"
            )
        document = self._json(response)
        identifier = document.get("id")
        if type(identifier) is not int or identifier <= 0:
            recovered = existing()
            if recovered is None:
                raise AmbiguousPublicationEffect(
                    "OpenProject acknowledged a comment without a verifiable marker"
                )
            return recovered
        return identifier

    def _custom_values(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        mapping = {
            "plan_id": "plan_id",
            "node_key": "node_key",
            "plan_version": "plan_version",
            "managed_hash": "managed_hash",
            "repository": "repository",
            "risk": "risk",
            "agent_eligible": "agent_eligibility",
            "source_requirements": "source_requirements",
            "planning_commit": "planning_commit",
            "evidence_state": "evidence_state",
        }
        for semantic, source in mapping.items():
            if source not in payload:
                continue
            value = payload[source]
            if semantic == "agent_eligible":
                if not isinstance(value, bool):
                    raise OpenProjectPublicationError("agent eligibility must be a boolean")
            elif semantic == "evidence_state":
                if not isinstance(value, str) or not value:
                    raise OpenProjectPublicationError("evidence state must be a non-empty string")
            elif semantic == "source_requirements":
                if not isinstance(value, (list, tuple)) or not all(
                    isinstance(item, str) for item in value
                ):
                    raise OpenProjectPublicationError(
                        "source requirements must be a string sequence"
                    )
                value = {
                    "raw": json.dumps(
                        list(value),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                }
            values[self._field_name(semantic)] = value
        return values

    def _work_package_payload(
        self, operation: PublicationOperation, raw: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        payload = dict(operation.payload)
        result = self._custom_values(payload)
        if operation.managed_after_hash is not None:
            result[self._field_name("managed_hash")] = operation.managed_after_hash
        links: dict[str, Any] = {}
        if operation.kind in {"create_work_package", "update_managed_fields"}:
            result["subject"] = payload["title"]
            generated = str(payload["generated_description"])
            result["description"] = {
                "raw": replace_generated_description(self._description(raw or {}), generated)
            }
            type_id = self.config.type_ids.get(str(payload.get("work_package_type", "")), 0)
            if not type_id:
                raise OpenProjectPublicationError("operation lacks a configured work-package type")
            links["type"] = self._link("types", type_id)
            if operation.kind == "create_work_package":
                links["project"] = self._link("projects", self.config.project_id)
            risk = str(payload.get("risk", ""))
            if risk not in self.config.priority_ids:
                raise OpenProjectPublicationError("operation lacks a configured risk priority")
            links["priority"] = self._link("priorities", self.config.priority_ids[risk])
        if links:
            result["_links"] = links
        return result

    def _validated_form(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        commit_paths: tuple[str, ...],
        methods: set[str],
    ) -> tuple[Mapping[str, Any], str, str]:
        response = self._request("POST", path, json=payload)
        form = self._json(response)
        errors = (
            form.get("_embedded", {}).get("validationErrors", [])
            if isinstance(form.get("_embedded"), Mapping)
            else []
        )
        if errors:
            raise OpenProjectPublicationError("OpenProject form validation rejected publication")
        embedded = form.get("_embedded")
        if not isinstance(embedded, Mapping):
            raise OpenProjectPublicationError("OpenProject form response has no embedded payload")
        validated = embedded.get("payload")
        if not isinstance(validated, Mapping):
            raise OpenProjectPublicationError("OpenProject form response has no validated payload")
        links = form.get("_links")
        if not isinstance(links, Mapping):
            raise OpenProjectPublicationError("OpenProject form has no commit link")
        method, target = self._safe_api_link(
            links.get("commit"), methods=methods, paths=commit_paths
        )
        return validated, method, target

    def _patch_with_one_conflict_retry(
        self,
        operation: PublicationOperation,
        resolved: _ResolvedPackage,
        payload_builder: Any,
    ) -> _ResolvedPackage:
        for attempt in range(2):
            current = resolved if attempt == 0 else self._resolve_fresh(operation.identity)
            if (
                operation.managed_before_hash is not None
                and current.snapshot.managed_hash != operation.managed_before_hash
            ):
                raise OpenProjectConflict("managed state changed before retry")
            self._assert_topology_preconditions(operation, current.snapshot)
            payload = dict(payload_builder(current.raw))
            payload["lockVersion"] = current.snapshot.lock_version
            validated, commit_method, commit_target = self._validated_form(
                f"{_API_PREFIX}/work_packages/{current.snapshot.id}/form",
                payload,
                commit_paths=(f"{_API_PREFIX}/work_packages/{current.snapshot.id}",),
                methods={"PATCH"},
            )
            validated = dict(validated)
            validated["lockVersion"] = current.snapshot.lock_version
            try:
                response = self._client.request(commit_method, commit_target, json=validated)
            except httpx.TransportError as error:
                try:
                    recovered = self.postcondition(operation)
                except OpenProjectPublicationError:
                    recovered = False
                if recovered:
                    return self._resolve_fresh(operation.identity)
                raise AmbiguousPublicationEffect(
                    "OpenProject update response was lost and the exact postcondition is absent"
                ) from error
            if response.status_code != 409:
                if response.status_code != 200:
                    if response.status_code == 408 or response.status_code >= 500:
                        with suppress(OpenProjectPublicationError):
                            if self.postcondition(operation):
                                return self._resolve_fresh(operation.identity)
                        raise AmbiguousPublicationEffect(
                            "OpenProject update returned a possibly committed server failure"
                        )
                    raise OpenProjectPublicationError(
                        f"OpenProject PATCH work package returned {response.status_code}"
                    )
                try:
                    body = self._json(response)
                    return _ResolvedPackage(self._work_package(body), body)
                except OpenProjectPublicationError as error:
                    with suppress(OpenProjectPublicationError):
                        if self.postcondition(operation):
                            return self._resolve_fresh(operation.identity)
                    raise AmbiguousPublicationEffect(
                        "OpenProject acknowledged update without a verifiable response"
                    ) from error
            identifier = ""
            with suppress(OpenProjectPublicationError):
                identifier = str(self._json(response).get("errorIdentifier", ""))
            if identifier != _UPDATE_CONFLICT:
                raise OpenProjectPublicationError("OpenProject returned an unexpected 409 response")
            if attempt == 1:
                raise OpenProjectConflict("OpenProject update conflict after one retry")
            # Exactly one retry. _resolve_fresh also proves identity did not move.
        raise AssertionError("unreachable")

    def _resolve_fresh(self, identity: tuple[str, str]) -> _ResolvedPackage:
        matches = self._identity_matches(identity)
        if not matches:
            raise OpenProjectPublicationError(
                f"identity cannot be resolved: {identity[0]}:{identity[1]}"
            )
        return matches[0]

    @staticmethod
    def _created_state_matches(
        operation: PublicationOperation, current: WorkPackageSnapshot
    ) -> bool:
        return (
            current.identity == operation.identity
            and current.managed_hash == operation.managed_after_hash
            and current.evidence_state == operation.payload.get("evidence_state")
            and current.parent_id is None
            and not current.managed_relations
            and not current.superseded
        )

    def _recover_created_id(self, operation: PublicationOperation) -> int | None:
        matches = self._identity_matches(operation.identity)
        if matches and self._created_state_matches(operation, matches[0].snapshot):
            return matches[0].snapshot.id
        return None

    def _apply_create(self, operation: PublicationOperation) -> int:
        if self.resolve(operation.identity) is not None:
            raise OpenProjectPublicationError("identity appeared before create")
        payload = self._work_package_payload(operation, None)
        validated, commit_method, commit_target = self._validated_form(
            f"{_API_PREFIX}/work_packages/form",
            payload,
            commit_paths=(f"{_API_PREFIX}/work_packages",),
            methods={"POST"},
        )
        try:
            response = self._client.request(commit_method, commit_target, json=validated)
        except httpx.TransportError as error:
            try:
                recovered_id = self._recover_created_id(operation)
            except OpenProjectPublicationError as recovery_error:
                if "duplicate publication identity" in str(recovery_error):
                    raise
                raise AmbiguousPublicationEffect(
                    "OpenProject create response and recovery read were both lost"
                ) from error
            if recovered_id is not None:
                return recovered_id
            raise AmbiguousPublicationEffect(
                "OpenProject create response was lost and the exact identity is not visible"
            ) from error
        if response.status_code != 201:
            if response.status_code == 408 or response.status_code >= 500:
                try:
                    recovered_id = self._recover_created_id(operation)
                    if recovered_id is not None:
                        return recovered_id
                except OpenProjectPublicationError as recovery_error:
                    if "duplicate publication identity" in str(recovery_error):
                        raise
                raise AmbiguousPublicationEffect(
                    "OpenProject create returned a possibly committed server failure"
                )
            raise OpenProjectPublicationError(f"OpenProject create returned {response.status_code}")
        try:
            created = self._work_package(self._json(response))
            if self._created_state_matches(operation, created):
                return created.id
        except OpenProjectPublicationError:
            pass
        try:
            recovered_id = self._recover_created_id(operation)
            if recovered_id is not None:
                return recovered_id
        except OpenProjectPublicationError as recovery_error:
            if "duplicate publication identity" in str(recovery_error):
                raise
        raise AmbiguousPublicationEffect(
            "OpenProject acknowledged create without the requested exact state"
        )

    def _apply_relation(self, operation: PublicationOperation, *, remove: bool) -> None:
        source = self._resolve_fresh(operation.identity)
        self._assert_topology_preconditions(operation, source.snapshot)
        semantic = str(operation.payload["type"])
        target_identity = tuple(operation.payload["target_identity"])
        if len(target_identity) != 2 or not all(isinstance(part, str) for part in target_identity):
            raise OpenProjectPublicationError("relation target identity is invalid")
        target = self._resolve_fresh((target_identity[0], target_identity[1]))
        from_id, to_id, native_type = self._project_relation(
            semantic, source.snapshot.id, target.snapshot.id
        )
        marker = self._relation_marker(
            semantic, operation.identity, (target_identity[0], target_identity[1])
        )
        matching: _Relation | None = None
        for relation in self._relations(source.snapshot.id):
            if relation.description == marker:
                if not self._relation_matches_marker(
                    relation,
                    semantic,
                    operation.identity,
                    (target_identity[0], target_identity[1]),
                ):
                    raise OpenProjectPublicationError(
                        "managed relation marker disagrees with native endpoints or type"
                    )
                matching = relation
                continue
            if (relation.from_id, relation.to_id, relation.relation_type) != (
                from_id,
                to_id,
                native_type,
            ):
                continue
            raise OpenProjectPublicationError("human/native relation semantic collision")
        if remove:
            if matching is not None:
                try:
                    response = self._client.request(
                        "DELETE", f"{_API_PREFIX}/relations/{matching.id}"
                    )
                except httpx.TransportError as error:
                    with suppress(OpenProjectPublicationError):
                        if self.postcondition(operation):
                            return
                    raise AmbiguousPublicationEffect(
                        "OpenProject relation deletion response was lost"
                    ) from error
                if response.status_code != 204:
                    if response.status_code == 408 or response.status_code >= 500:
                        with suppress(OpenProjectPublicationError):
                            if self.postcondition(operation):
                                return
                        raise AmbiguousPublicationEffect(
                            "OpenProject relation deletion returned a possibly committed server failure"
                        )
                    raise OpenProjectPublicationError(
                        f"OpenProject DELETE relation returned {response.status_code}"
                    )
                if not self.postcondition(operation):
                    raise AmbiguousPublicationEffect(
                        "OpenProject acknowledged relation deletion without its exact postcondition"
                    )
            return
        if matching is None:
            try:
                response = self._client.request(
                    "POST",
                    f"{_API_PREFIX}/work_packages/{from_id}/relations",
                    json={
                        "type": native_type,
                        "description": marker,
                        "_links": {"to": self._link("work_packages", to_id)},
                    },
                )
            except httpx.TransportError as error:
                with suppress(OpenProjectPublicationError):
                    if self.postcondition(operation):
                        return
                raise AmbiguousPublicationEffect(
                    "OpenProject relation creation response was lost"
                ) from error
            if response.status_code != 201:
                if response.status_code == 408 or response.status_code >= 500:
                    with suppress(OpenProjectPublicationError):
                        if self.postcondition(operation):
                            return
                    raise AmbiguousPublicationEffect(
                        "OpenProject relation creation returned a possibly committed server failure"
                    )
                raise OpenProjectPublicationError(
                    f"OpenProject POST relation returned {response.status_code}"
                )
            if not self.postcondition(operation):
                raise AmbiguousPublicationEffect(
                    "OpenProject acknowledged relation creation without its exact postcondition"
                )

    def apply(
        self,
        operation: PublicationOperation,
        *,
        idempotency_key: str,
        current: WorkPackageSnapshot | None,
    ) -> None:
        if idempotency_key != operation.operation_id:
            raise OpenProjectPublicationError(
                "idempotency key does not match publication operation"
            )
        if operation.kind == "record_audit":
            self.effects.append(
                PublicationEffect(
                    operation.operation_id, operation.kind, operation.identity, "recorded"
                )
            )
            return
        if operation.kind == "create_work_package":
            package_id = self._apply_create(operation)
            self.effects.append(
                PublicationEffect(
                    operation.operation_id,
                    operation.kind,
                    operation.identity,
                    "applied",
                    package_id,
                )
            )
            return
        if operation.kind == "create_relation":
            self._apply_relation(operation, remove=False)
            self.effects.append(
                PublicationEffect(
                    operation.operation_id, operation.kind, operation.identity, "applied"
                )
            )
            return
        if operation.kind == "remove_managed_relation":
            self._apply_relation(operation, remove=True)
            self.effects.append(
                PublicationEffect(
                    operation.operation_id, operation.kind, operation.identity, "applied"
                )
            )
            return
        resolved = self._resolve_fresh(operation.identity)
        if current is not None and current.managed_hash != resolved.snapshot.managed_hash:
            raise OpenProjectConflict("managed state changed after publisher refresh")
        self._assert_topology_preconditions(operation, resolved.snapshot)
        if operation.kind == "update_managed_fields":
            result = self._patch_with_one_conflict_retry(
                operation, resolved, lambda raw: self._work_package_payload(operation, raw)
            )
        elif operation.kind == "set_parent":
            parent = operation.payload["parent_identity"]
            parent_link = None
            if parent is not None:
                parent_package = self._resolve_fresh((parent[0], parent[1]))
                parent_link = self._link("work_packages", parent_package.snapshot.id)
            result = self._patch_with_one_conflict_retry(
                operation, resolved, lambda _raw: {"_links": {"parent": parent_link}}
            )
        elif operation.kind == "mark_superseded":
            result = self._patch_with_one_conflict_retry(
                operation,
                resolved,
                lambda _raw: {
                    "_links": {
                        "status": self._link("statuses", self.config.status_ids["Superseded"])
                    }
                },
            )
        elif operation.kind == "reactivate_work_package":
            if operation.payload.get("status") != "Ready":
                raise OpenProjectPublicationError(
                    "reactivation requires the configured Ready status"
                )
            result = self._patch_with_one_conflict_retry(
                operation,
                resolved,
                lambda _raw: {
                    "_links": {"status": self._link("statuses", self.config.status_ids["Ready"])}
                },
            )
        else:
            raise OpenProjectPublicationError(
                f"unsupported publication operation: {operation.kind}"
            )
        if not self.postcondition(operation):
            raise AmbiguousPublicationEffect(
                "OpenProject update did not reach its exact postcondition"
            )
        self.effects.append(
            PublicationEffect(
                operation.operation_id,
                operation.kind,
                operation.identity,
                "applied",
                result.snapshot.id,
            )
        )

    def postcondition(self, operation: PublicationOperation) -> bool:
        """Read-only recovery gate for an intent that may have reached OpenProject."""
        if operation.kind == "record_audit":
            return False
        current = self.resolve(operation.identity)
        if operation.kind == "create_work_package":
            return current is not None and self._created_state_matches(operation, current)
        if current is None:
            return False
        topology_matches = self._postcondition_topology(operation, current)
        if operation.kind == "update_managed_fields":
            return current.managed_hash == operation.managed_after_hash and topology_matches
        if operation.kind == "mark_superseded":
            return current.superseded and topology_matches
        if operation.kind == "reactivate_work_package":
            return (
                operation.payload.get("status") == "Ready"
                and current.human_fields.get("status_id") == self.config.status_ids["Ready"]
                and not current.superseded
                and topology_matches
            )
        if operation.kind == "set_parent":
            return topology_matches
        if operation.kind in {"create_relation", "remove_managed_relation"}:
            return topology_matches
        return False
