# ruff: noqa: E501
from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from planning_platform.diff import PublicationOperation, plan_diff
from planning_platform.loader import load_artifact
from planning_platform.openproject import canonical_hash, replace_generated_description
from planning_platform.openproject_adapter import (
    OpenProjectAdapterConfig,
    OpenProjectConflict,
    OpenProjectPublicationAdapter,
    OpenProjectPublicationError,
)
from planning_platform.publication_journal import (
    AmbiguousPublicationEffect,
    InMemoryPublicationJournal,
)
from planning_platform.publisher import PublicationEnvelope, publish


def _config() -> OpenProjectAdapterConfig:
    return OpenProjectAdapterConfig(
        base_url="https://op.test",
        project_id=42,
        type_ids={
            "Idea": 10,
            "Initiative": 11,
            "Epic": 12,
            "Story": 13,
            "Task": 14,
            "Decision": 15,
            "Investigation": 16,
            "Bug": 17,
        },
        status_ids={
            "Draft": 20,
            "Planning": 21,
            "Needs Input": 22,
            "Proposed": 23,
            "Ready": 24,
            "In Progress": 25,
            "Blocked": 26,
            "Review": 27,
            "Done": 28,
            "Superseded": 29,
            "Rejected": 30,
        },
        custom_field_ids={
            "plan_id": 40,
            "node_key": 41,
            "plan_version": 42,
            "managed_hash": 43,
            "repository": 44,
            "risk": 45,
            "agent_eligible": 46,
            "source_requirements": 47,
            "planning_commit": 48,
            "evidence_state": 49,
        },
        priority_ids={"low": 50, "medium": 51, "high": 52, "critical": 53},
    )


def _projection(
    *,
    node: str = "node",
    title: str = "Human-safe title",
    generated: str = "<!-- planning-platform:generated -->\nold\n<!-- /planning-platform:generated -->",
) -> dict[str, Any]:
    return {
        "title": title,
        "work_package_type": "Task",
        "generated_description": generated,
        "priority": "low",
        "risk": "low",
        "repository": "example/repo",
        "source_requirements": ["REQ-1"],
        "plan_id": "plan-a",
        "node_key": node,
        "plan_version": 1,
        "agent_eligibility": True,
        "planning_commit": "a" * 40,
    }


def _raw(
    package_id: int,
    *,
    node: str = "node",
    managed_hash: str | None = None,
    lock: int = 3,
    title: str = "Human-safe title",
    generated: str = "<!-- planning-platform:generated -->\nold\n<!-- /planning-platform:generated -->",
) -> dict[str, Any]:
    projection = _projection(node=node, title=title, generated=generated)
    return {
        "id": package_id,
        "subject": title,
        "lockVersion": lock,
        "description": {"raw": f"Human introduction\n\n{generated}\n\nHuman closing"},
        "customField40": "plan-a",
        "customField41": node,
        "customField42": 1,
        "customField43": managed_hash or canonical_hash(projection),
        "customField44": "example/repo",
        "customField45": "low",
        "customField46": True,
        "customField47": {
            "format": "markdown",
            "raw": '["REQ-1"]',
            "html": "<p>[&quot;REQ-1&quot;]</p>",
        },
        "customField48": "a" * 40,
        "customField49": "pending",
        "_links": {
            "type": {"href": "/api/v3/types/14"},
            "priority": {"href": "/api/v3/priorities/50"},
            "status": {"href": "/api/v3/statuses/25"},
        },
    }


_BEFORE_HASH = canonical_hash(_projection())
_AFTER_GENERATED = (
    "<!-- planning-platform:generated -->\nnew\n<!-- /planning-platform:generated -->"
)
_AFTER_HASH = canonical_hash(_projection(title="Generated title", generated=_AFTER_GENERATED))


def _adapter(handler: httpx.MockTransport) -> OpenProjectPublicationAdapter:
    def routed(request: httpx.Request) -> httpx.Response:
        try:
            return handler.handle_request(request)
        except (AssertionError, ValueError):
            if request.url.path == "/api/v3/relations":
                return _collection([])
            raise

    client = httpx.Client(
        base_url="https://op.test",
        transport=httpx.MockTransport(routed),
        auth=httpx.BasicAuth("apikey", "token-not-to-be-logged"),
        timeout=httpx.Timeout(1.0),
    )
    return OpenProjectPublicationAdapter(_config(), "token-not-to-be-logged", client=client)


def _topology(
    *,
    parent: tuple[str, str] | None = None,
    unmanaged_parent: bool = False,
    relations: tuple[tuple[str, tuple[str, str]], ...] = (),
) -> dict[str, Any]:
    return {
        "expected_parent_identity": parent,
        "expected_unmanaged_parent": unmanaged_parent,
        "expected_managed_relations": [list(relation) for relation in relations],
    }


def _operation(kind: str = "update_managed_fields") -> PublicationOperation:
    return PublicationOperation(
        operation_id="operation-1",
        kind=kind,  # type: ignore[arg-type]
        identity=("plan-a", "node"),
        preconditions=_topology(),
        managed_before_hash=_BEFORE_HASH,
        managed_after_hash=_AFTER_HASH,
        trace_id="trace",
        payload={
            "title": "Generated title",
            "work_package_type": "Task",
            "generated_description": _AFTER_GENERATED,
            "plan_id": "plan-a",
            "node_key": "node",
            "plan_version": 1,
            "managed_hash": _AFTER_HASH,
            "repository": "example/repo",
            "risk": "low",
            "agent_eligibility": True,
            "source_requirements": ["REQ-1"],
            "planning_commit": "a" * 40,
        },
    )


def _collection(
    values: list[dict[str, Any]],
    *,
    offset: int = 1,
    total: int | None = None,
    next_href: str | None = None,
) -> httpx.Response:
    links: dict[str, object] = {}
    if next_href is not None:
        links["nextByOffset"] = {"href": next_href, "method": "get"}
    return httpx.Response(
        200,
        json={
            "count": len(values),
            "total": len(values) if total is None else total,
            "offset": offset,
            "_links": links,
            "_embedded": {"elements": values},
        },
    )


def _form(payload: dict[str, Any], *, href: str, method: str) -> httpx.Response:
    """Captured v17.6 form shape; commit is authoritative, not guessed by client."""
    return httpx.Response(
        200,
        json={
            "_type": "Form",
            "_embedded": {"validationErrors": [], "payload": payload},
            "_links": {"commit": {"href": href, "method": method}},
        },
    )


def test_identity_scan_is_paginated_authenticates_and_duplicate_is_fatal() -> None:
    seen_auth: list[str] = []
    raw = _raw(2)

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization", ""))
        if request.url.path.endswith("/projects/42/work_packages"):
            if request.url.params["offset"] == "1":
                return _collection(
                    [_raw(index, node=f"other-{index}") for index in range(1, 101)],
                    total=101,
                    next_href="/api/v3/projects/42/work_packages?pageSize=100&offset=2",
                )
            return _collection([raw], offset=2, total=101)
        if request.url.path == "/api/v3/work_packages/2":
            return httpx.Response(200, json=raw)
        raise AssertionError(request.url)

    adapter = _adapter(httpx.MockTransport(handler))
    resolved = adapter.resolve(("plan-a", "node"))
    assert resolved is not None and resolved.id == 2
    assert len(seen_auth) == 4
    assert seen_auth[0] == "Basic " + base64.b64encode(b"apikey:token-not-to-be-logged").decode()

    def duplicate_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([_raw(1), _raw(2)])
        return httpx.Response(200, json=_raw(int(request.url.path.rsplit("/", 1)[-1])))

    with pytest.raises(OpenProjectPublicationError, match="duplicate publication identity"):
        _adapter(httpx.MockTransport(duplicate_handler)).resolve(("plan-a", "node"))


def test_pagination_allows_missing_read_method_but_forms_remain_strict() -> None:
    raw = _raw(2)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            if request.url.params["offset"] == "1":
                return httpx.Response(
                    200,
                    json={
                        "count": 1,
                        "total": 2,
                        "offset": 1,
                        "_links": {
                            "nextByOffset": {
                                "href": "/api/v3/projects/42/work_packages?pageSize=100&offset=2"
                            }
                        },
                        "_embedded": {"elements": [_raw(1, node="other")]},
                    },
                )
            return _collection([raw], offset=2, total=2)
        if request.url.path == "/api/v3/work_packages/2":
            return httpx.Response(200, json=raw)
        raise AssertionError(request.url)

    assert _adapter(httpx.MockTransport(handler)).resolve(("plan-a", "node")) is not None
    adapter = _adapter(httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    with pytest.raises(OpenProjectPublicationError, match="commit method"):
        adapter._safe_api_link(
            {"href": "/api/v3/work_packages/1"},
            methods={"PATCH"},
            paths=("/api/v3/work_packages/1",),
        )


def test_pagination_validates_every_page_before_following_next_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 1,
                "total": 0,
                "offset": 0,
                "_links": {
                    "nextByOffset": {
                        "href": ("/api/v3/projects/42/work_packages?pageSize=100&offset=2")
                    }
                },
                "_embedded": {"elements": [_raw(1)]},
            },
        )

    with pytest.raises(OpenProjectPublicationError, match="metadata is inconsistent"):
        _adapter(httpx.MockTransport(handler)).resolve(("plan-a", "node"))


def test_pagination_without_next_link_must_make_bounded_progress() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "count": 1 if calls == 1 else 0,
                "total": 2,
                "offset": 1,
                "_links": {},
                "_embedded": {"elements": [_raw(1, node="other")] if calls == 1 else []},
            },
        )

    with pytest.raises(OpenProjectPublicationError, match="metadata is inconsistent"):
        _adapter(httpx.MockTransport(handler)).resolve(("plan-a", "node"))
    assert calls == 2


def test_pagination_next_link_cannot_skip_a_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 1,
                "total": 2,
                "offset": 1,
                "_links": {
                    "nextByOffset": {
                        "href": ("/api/v3/projects/42/work_packages?pageSize=100&offset=3")
                    }
                },
                "_embedded": {"elements": [_raw(1, node="other")]},
            },
        )

    with pytest.raises(
        OpenProjectPublicationError,
        match="does not preserve collection scope",
    ):
        _adapter(httpx.MockTransport(handler)).resolve(("plan-a", "node"))


def test_partial_identity_and_invalid_priority_configuration_fail_closed() -> None:
    raw = _raw(1)
    raw["customField41"] = None
    adapter = _adapter(httpx.MockTransport(lambda request: httpx.Response(500)))
    with pytest.raises(OpenProjectPublicationError, match="Plan ID and Node key"):
        adapter._work_package(raw)
    with pytest.raises(ValueError, match="priority_ids"):
        replace(_config(), priority_ids={"low": 50, "medium": 50, "high": 52, "critical": 53})
    with pytest.raises(ValueError, match="priority_ids"):
        replace(_config(), priority_ids={"low": 50})
    with pytest.raises(ValueError, match="collection bounds"):
        replace(_config(), max_collection_pages=0)
    with pytest.raises(ValueError, match="collection bounds"):
        replace(_config(), max_collection_items=10, page_size=100)


@pytest.mark.parametrize("lock_version", [None, True, -1, "3"])
def test_work_package_requires_observed_nonnegative_integer_lock_version(
    lock_version: object,
) -> None:
    raw = _raw(1)
    if lock_version is None:
        raw.pop("lockVersion")
    else:
        raw["lockVersion"] = lock_version
    with pytest.raises(OpenProjectPublicationError, match="lockVersion"):
        _adapter(httpx.MockTransport(lambda request: httpx.Response(500)))._work_package(raw)


def test_collection_total_and_page_count_are_configuration_bounded() -> None:
    def oversized(request: httpx.Request) -> httpx.Response:
        return _collection([_raw(1)], total=10_001)

    with pytest.raises(OpenProjectPublicationError, match="metadata is inconsistent"):
        _adapter(httpx.MockTransport(oversized)).resolve(("plan-a", "node"))

    config = replace(_config(), max_collection_pages=1)

    def two_pages(request: httpx.Request) -> httpx.Response:
        return _collection(
            [_raw(1, node="other")],
            offset=int(request.url.params["offset"]),
            total=2,
        )

    client = httpx.Client(
        base_url="https://op.test",
        transport=httpx.MockTransport(two_pages),
        auth=httpx.BasicAuth("apikey", "token-not-to-be-logged"),
    )
    adapter = OpenProjectPublicationAdapter(
        config,
        "token-not-to-be-logged",
        client=client,
    )
    with pytest.raises(OpenProjectPublicationError, match="page bound"):
        adapter.resolve(("plan-a", "node"))


def test_read_redirect_is_not_accepted_as_an_api_document() -> None:
    adapter = _adapter(
        httpx.MockTransport(
            lambda request: httpx.Response(
                302,
                headers={"Location": "/api/v3/projects/42/work_packages"},
            )
        )
    )
    with pytest.raises(OpenProjectPublicationError, match="returned 302"):
        adapter.resolve(("plan-a", "node"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("agent_eligibility", "true", "eligibility must be a boolean"),
        ("source_requirements", "REQ-1", "requirements must be a string sequence"),
        ("source_requirements", ["REQ-1", 2], "requirements must be a string sequence"),
    ],
)
def test_malformed_managed_custom_values_fail_before_a_write(
    field: str, value: object, message: str
) -> None:
    writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        writes += 1
        raise AssertionError(request.url)

    operation = _operation()
    operation = PublicationOperation(
        **{
            **operation.__dict__,
            "payload": {**operation.payload, field: value},
        }
    )
    adapter = _adapter(httpx.MockTransport(handler))
    with pytest.raises(OpenProjectPublicationError, match=message):
        adapter._work_package_payload(operation, _raw(1))
    assert writes == 0


def test_source_requirements_use_v176_text_shape_and_round_trip_newlines() -> None:
    requirements = ["REQ-1 first line\nsecond line", "REQ-2"]
    operation = _operation()
    operation = PublicationOperation(
        **{
            **operation.__dict__,
            "payload": {
                **operation.payload,
                "source_requirements": requirements,
            },
        }
    )
    adapter = _adapter(
        httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError(request)))
    )
    custom_values = adapter._custom_values(operation.payload)
    encoded = json.dumps(requirements, ensure_ascii=False, separators=(",", ":"))
    assert custom_values["customField47"] == {"raw": encoded}

    raw = _raw(1)
    raw["customField47"] = {
        "format": "markdown",
        "raw": encoded,
        "html": "<p>rendered by OpenProject</p>",
    }
    projection = {
        **_projection(),
        "source_requirements": requirements,
    }
    raw["customField43"] = canonical_hash(projection)
    assert adapter._work_package(raw).managed_hash == canonical_hash(projection)


def test_generated_description_bounds_preserve_human_text_and_reject_malformed() -> None:
    generated = "<!-- planning-platform:generated -->\nnew\n<!-- /planning-platform:generated -->"
    result = replace_generated_description(
        "before\n<!-- planning-platform:generated -->\nold\n<!-- /planning-platform:generated -->\nafter",
        generated,
    )
    assert result == f"before\n{generated}\nafter"
    assert replace_generated_description("human", generated) == f"human\n\n{generated}"
    with pytest.raises(ValueError, match="markers"):
        replace_generated_description("<!-- planning-platform:generated --> orphan", generated)


def test_form_validation_errors_are_rejected_without_patch() -> None:
    raw = _raw(1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([raw])
        if request.url.path == "/api/v3/work_packages/1":
            return httpx.Response(200, json=raw)
        if request.url.path.endswith("/form"):
            return httpx.Response(
                200, json={"_embedded": {"validationErrors": [{"message": "no"}], "payload": {}}}
            )
        if request.method == "PATCH":
            raise AssertionError("invalid form must not patch")
        raise AssertionError(request.url)

    adapter = _adapter(httpx.MockTransport(handler))
    with pytest.raises(OpenProjectPublicationError, match="form validation"):
        adapter.apply(
            _operation(), idempotency_key="operation-1", current=adapter.resolve(("plan-a", "node"))
        )


def test_update_uses_fresh_lock_version_and_exactly_one_conflict_retry() -> None:
    raw = _raw(1)
    calls = {"patch": 0, "get": 0}
    lock_versions: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([raw])
        if request.url.path == "/api/v3/work_packages/1" and request.method == "GET":
            calls["get"] += 1
            return httpx.Response(200, json={**raw, "lockVersion": 3 + (calls["get"] >= 3)})
        if request.url.path.endswith("/form"):
            return _form(
                json.loads(request.content), href="/api/v3/work_packages/1", method="patch"
            )
        if request.method == "PATCH":
            calls["patch"] += 1
            lock_versions.append(json.loads(request.content)["lockVersion"])
            if calls["patch"] == 1:
                return httpx.Response(
                    409,
                    json={
                        "_type": "Error",
                        "errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict",
                    },
                )
            updated = _raw(
                1,
                lock=4,
                title="Generated title",
                generated=_AFTER_GENERATED,
            )
            raw.clear()
            raw.update(updated)
            return httpx.Response(200, json=raw)
        raise AssertionError(request.url)

    adapter = _adapter(httpx.MockTransport(handler))
    current = adapter.resolve(("plan-a", "node"))
    adapter.apply(_operation(), idempotency_key="operation-1", current=current)
    assert calls["patch"] == 2
    assert lock_versions == [3, 4]
    assert adapter.effects[-1].outcome == "applied"


@pytest.mark.parametrize("persisted", [True, False])
def test_update_transport_loss_requires_exact_postcondition(persisted: bool) -> None:
    state = _raw(1)
    patch_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal patch_attempts
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([state])
        if request.url.path == "/api/v3/work_packages/1" and request.method == "GET":
            return httpx.Response(200, json=state)
        if request.url.path.endswith("/form"):
            return _form(
                json.loads(request.content),
                href="/api/v3/work_packages/1",
                method="patch",
            )
        if request.url.path == "/api/v3/work_packages/1" and request.method == "PATCH":
            patch_attempts += 1
            if persisted:
                payload = json.loads(request.content)
                state.update(payload)
                state["_links"] = {
                    **state["_links"],
                    **payload.get("_links", {}),
                }
                state["lockVersion"] = 4
            raise httpx.ReadTimeout("lost response", request=request)
        raise AssertionError(request.url)

    adapter = _adapter(httpx.MockTransport(handler))
    current = adapter.resolve(("plan-a", "node"))
    if persisted:
        adapter.apply(_operation(), idempotency_key="operation-1", current=current)
        assert adapter.effects[-1].outcome == "applied"
    else:
        with pytest.raises(AmbiguousPublicationEffect, match="exact postcondition is absent"):
            adapter.apply(_operation(), idempotency_key="operation-1", current=current)
    assert patch_attempts == 1


def test_conflict_refuses_retry_when_managed_hash_changes() -> None:
    raw = _raw(1)
    calls = {"get": 0, "patch": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([raw])
        if request.url.path == "/api/v3/work_packages/1" and request.method == "GET":
            calls["get"] += 1
            changed = calls["get"] >= 3
            return httpx.Response(
                200,
                json=_raw(1, title="Concurrent generated title") if changed else raw,
            )
        if request.url.path.endswith("/form"):
            return _form(
                json.loads(request.content), href="/api/v3/work_packages/1", method="patch"
            )
        if request.method == "PATCH":
            calls["patch"] += 1
            return httpx.Response(
                409, json={"errorIdentifier": "urn:openproject-org:api:v3:errors:UpdateConflict"}
            )
        raise AssertionError(request.url)

    adapter = _adapter(httpx.MockTransport(handler))
    with pytest.raises(OpenProjectConflict, match="managed state changed"):
        adapter.apply(
            _operation(), idempotency_key="operation-1", current=adapter.resolve(("plan-a", "node"))
        )
    assert calls["patch"] == 1


def test_ambiguous_create_transport_failure_recovers_by_identity_once() -> None:
    raw = _raw(
        9,
        title="Generated title",
        generated=_AFTER_GENERATED,
    )
    created_attempts = 0
    scans = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created_attempts, scans
        if request.url.path.endswith("/projects/42/work_packages"):
            scans += 1
            return _collection([] if scans == 1 else [raw])
        if request.url.path == "/api/v3/work_packages/form":
            return _form(json.loads(request.content), href="/api/v3/work_packages", method="post")
        if request.url.path == "/api/v3/work_packages" and request.method == "POST":
            created_attempts += 1
            raise httpx.ReadTimeout("lost response", request=request)
        if request.url.path == "/api/v3/work_packages/9":
            return httpx.Response(200, json=raw)
        raise AssertionError(request.url)

    operation = _operation("create_work_package")
    operation = PublicationOperation(
        **{
            **operation.__dict__,
            "managed_before_hash": None,
            "payload": {
                **operation.payload,
                "work_package_type": "Task",
                "evidence_state": "pending",
            },
        }
    )
    adapter = _adapter(httpx.MockTransport(handler))
    adapter.apply(operation, idempotency_key="operation-1", current=None)
    assert created_attempts == 1
    assert adapter.effects[-1].work_package_id == 9


def test_ambiguous_create_transport_failure_without_identity_is_not_retried() -> None:
    created_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created_attempts
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([])
        if request.url.path == "/api/v3/work_packages/form":
            return _form(
                json.loads(request.content),
                href="/api/v3/work_packages",
                method="post",
            )
        if request.url.path == "/api/v3/work_packages" and request.method == "POST":
            created_attempts += 1
            raise httpx.ReadTimeout("lost response", request=request)
        raise AssertionError(request.url)

    operation = _operation("create_work_package")
    operation = PublicationOperation(
        **{
            **operation.__dict__,
            "managed_before_hash": None,
            "payload": {
                **operation.payload,
                "work_package_type": "Task",
                "evidence_state": "pending",
            },
        }
    )
    adapter = _adapter(httpx.MockTransport(handler))
    with pytest.raises(AmbiguousPublicationEffect, match="exact identity is not visible"):
        adapter.apply(operation, idempotency_key="operation-1", current=None)
    assert created_attempts == 1


def test_create_requires_exact_201_response() -> None:
    created = _raw(9, title="Generated title", generated=_AFTER_GENERATED)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([])
        if request.url.path == "/api/v3/work_packages/form":
            return _form(
                json.loads(request.content),
                href="/api/v3/work_packages",
                method="post",
            )
        if request.url.path == "/api/v3/work_packages" and request.method == "POST":
            return httpx.Response(200, json=created)
        raise AssertionError(request.url)

    operation = _operation("create_work_package")
    operation = PublicationOperation(
        **{
            **operation.__dict__,
            "managed_before_hash": None,
            "payload": {**operation.payload, "evidence_state": "pending"},
        }
    )
    with pytest.raises(OpenProjectPublicationError, match="create returned 200"):
        _adapter(httpx.MockTransport(handler)).apply(
            operation,
            idempotency_key="operation-1",
            current=None,
        )


def test_create_rejects_success_response_that_drops_evidence_state() -> None:
    created = _raw(9, title="Generated title", generated=_AFTER_GENERATED)
    created.pop("customField49")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([])
        if request.url.path == "/api/v3/work_packages/form":
            return _form(
                json.loads(request.content),
                href="/api/v3/work_packages",
                method="post",
            )
        if request.url.path == "/api/v3/work_packages" and request.method == "POST":
            return httpx.Response(201, json=created)
        raise AssertionError(request.url)

    operation = _operation("create_work_package")
    operation = PublicationOperation(
        **{
            **operation.__dict__,
            "managed_before_hash": None,
            "payload": {**operation.payload, "evidence_state": "pending"},
        }
    )
    with pytest.raises(AmbiguousPublicationEffect, match="requested exact state"):
        _adapter(httpx.MockTransport(handler)).apply(
            operation,
            idempotency_key=operation.operation_id,
            current=None,
        )


def test_update_requires_exact_200_response() -> None:
    raw = _raw(1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([raw])
        if request.url.path == "/api/v3/work_packages/1" and request.method == "GET":
            return httpx.Response(200, json=raw)
        if request.url.path.endswith("/form"):
            return _form(
                json.loads(request.content),
                href="/api/v3/work_packages/1",
                method="patch",
            )
        if request.url.path == "/api/v3/work_packages/1" and request.method == "PATCH":
            return httpx.Response(201, json=raw)
        raise AssertionError(request.url)

    adapter = _adapter(httpx.MockTransport(handler))
    with pytest.raises(OpenProjectPublicationError, match="PATCH work package returned 201"):
        adapter.apply(
            _operation(),
            idempotency_key="operation-1",
            current=adapter.resolve(("plan-a", "node")),
        )


def test_possibly_committed_server_failure_is_ambiguous_without_postcondition() -> None:
    raw = _raw(1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([raw])
        if request.url.path == "/api/v3/work_packages/1" and request.method == "GET":
            return httpx.Response(200, json=raw)
        if request.url.path.endswith("/form"):
            return _form(
                json.loads(request.content),
                href="/api/v3/work_packages/1",
                method="patch",
            )
        if request.url.path == "/api/v3/work_packages/1" and request.method == "PATCH":
            return httpx.Response(503)
        raise AssertionError(request.url)

    adapter = _adapter(httpx.MockTransport(handler))
    with pytest.raises(AmbiguousPublicationEffect, match="possibly committed"):
        adapter.apply(
            _operation(),
            idempotency_key="operation-1",
            current=adapter.resolve(("plan-a", "node")),
        )


def test_relation_projection_marker_safety_and_supersede_never_deletes_work_package() -> None:
    adapter = _adapter(httpx.MockTransport(lambda request: httpx.Response(500)))
    assert adapter._project_relation("blocked_by", 10, 20) == (20, 10, "blocks")
    assert adapter._project_relation("sequence_after", 10, 20) == (10, 20, "follows")
    assert adapter._project_relation("related_to", 20, 10) == (10, 20, "relates")
    marker = adapter._relation_marker("governed_by", ("plan", "a"), ("plan", "b"))
    assert adapter._parse_relation_marker(marker) == ("governed_by", ("plan", "a"), ("plan", "b"))
    assert adapter._parse_relation_marker(f"human {marker}") is None

    bootstrap = Path("openproject/bootstrap/17.6.0/bootstrap.rb").read_text()
    assert 'EXPECTED_OPENPROJECT_VERSION = "17.6.0"' in bootstrap
    assert "OPENPROJECT_API_TOKEN_FILE" in bootstrap
    assert "puts token" not in bootstrap and "puts webhook_secret" not in bootstrap
    assert "Webhook" in bootstrap and "Idea intake" in bootstrap


def test_relation_write_uses_reverse_direction_and_exact_marker_only() -> None:
    source = _raw(1)
    target = _raw(2, node="target")
    writes: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([source, target])
        if request.url.path == "/api/v3/work_packages/1":
            return httpx.Response(200, json=source)
        if request.url.path == "/api/v3/work_packages/2":
            return httpx.Response(200, json=target)
        if request.url.path == "/api/v3/relations":
            return _collection(relations)
        if request.url.path == "/api/v3/work_packages/2/relations":
            body = json.loads(request.content)
            writes.append(body)
            relations.append(
                {
                    "id": 99,
                    "type": body["type"],
                    "description": body["description"],
                    "_links": {
                        "from": {"href": "/api/v3/work_packages/2"},
                        "to": body["_links"]["to"],
                    },
                }
            )
            return httpx.Response(201, json={"id": 99})
        raise AssertionError(request.url)

    operation = PublicationOperation(
        operation_id="relation-1",
        kind="create_relation",
        identity=("plan-a", "node"),
        preconditions=_topology(),
        managed_before_hash=source["customField43"],
        managed_after_hash=source["customField43"],
        trace_id="trace",
        payload={"type": "blocked_by", "target_identity": ("plan-a", "target")},
    )
    adapter = _adapter(httpx.MockTransport(handler))
    adapter.apply(operation, idempotency_key="relation-1", current=None)
    assert writes == [
        {
            "type": "blocks",
            "_links": {"to": {"href": "/api/v3/work_packages/1"}},
            "description": adapter._relation_marker(
                "blocked_by", ("plan-a", "node"), ("plan-a", "target")
            ),
        }
    ]


def test_relation_create_requires_exact_201_response() -> None:
    source = _raw(1)
    target = _raw(2, node="target")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([source, target])
        if request.url.path == "/api/v3/work_packages/1":
            return httpx.Response(200, json=source)
        if request.url.path == "/api/v3/work_packages/2":
            return httpx.Response(200, json=target)
        if request.url.path == "/api/v3/relations":
            return _collection([])
        if request.url.path == "/api/v3/work_packages/2/relations":
            return httpx.Response(200, json={"id": 99})
        raise AssertionError(request.url)

    operation = PublicationOperation(
        operation_id="relation-exact-status",
        kind="create_relation",
        identity=("plan-a", "node"),
        preconditions=_topology(),
        managed_before_hash=source["customField43"],
        managed_after_hash=source["customField43"],
        trace_id="trace",
        payload={"type": "blocked_by", "target_identity": ("plan-a", "target")},
    )
    with pytest.raises(OpenProjectPublicationError, match="POST relation returned 200"):
        _adapter(httpx.MockTransport(handler)).apply(
            operation,
            idempotency_key=operation.operation_id,
            current=None,
        )


def test_marker_without_canonical_native_endpoints_is_not_observed_as_managed() -> None:
    source = _raw(1)
    target = _raw(2, node="target")
    adapter: OpenProjectPublicationAdapter

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/relations":
            return _collection(
                [
                    {
                        "id": 9,
                        "type": "blocks",
                        "description": adapter._relation_marker(
                            "blocked_by", ("plan-a", "node"), ("plan-a", "target")
                        ),
                        "_links": {
                            "from": {"href": "/api/v3/work_packages/1"},
                            "to": {"href": "/api/v3/work_packages/2"},
                        },
                    }
                ]
            )
        if request.url.path == "/api/v3/work_packages/1":
            return httpx.Response(200, json=source)
        if request.url.path == "/api/v3/work_packages/2":
            return httpx.Response(200, json=target)
        raise AssertionError(request.url)

    adapter = _adapter(httpx.MockTransport(handler))
    with pytest.raises(OpenProjectPublicationError, match="marker disagrees"):
        adapter._managed_relations_for(1, ("plan-a", "node"))


def test_related_to_is_observed_only_on_its_canonical_marker_owner() -> None:
    lower = _raw(2, node="a")
    higher = _raw(1, node="z")
    adapter: OpenProjectPublicationAdapter

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/relations":
            return _collection(
                [
                    {
                        "id": 9,
                        "type": "relates",
                        "description": adapter._relation_marker(
                            "related_to",
                            ("plan-a", "a"),
                            ("plan-a", "z"),
                        ),
                        "_links": {
                            "from": {"href": "/api/v3/work_packages/1"},
                            "to": {"href": "/api/v3/work_packages/2"},
                        },
                    }
                ]
            )
        if request.url.path == "/api/v3/work_packages/1":
            return httpx.Response(200, json=higher)
        if request.url.path == "/api/v3/work_packages/2":
            return httpx.Response(200, json=lower)
        raise AssertionError(request.url)

    adapter = _adapter(httpx.MockTransport(handler))
    assert adapter._managed_relations_for(2, ("plan-a", "a")) == (("related_to", ("plan-a", "z")),)
    assert adapter._managed_relations_for(1, ("plan-a", "z")) == ()


@pytest.mark.parametrize(
    ("kind", "persisted"),
    [
        ("create_relation", True),
        ("create_relation", False),
        ("remove_managed_relation", True),
        ("remove_managed_relation", False),
    ],
)
def test_relation_transport_loss_requires_exact_postcondition(kind: str, persisted: bool) -> None:
    source = _raw(1)
    target = _raw(2, node="target")
    relations: list[dict[str, Any]] = []
    write_attempts = 0
    adapter: OpenProjectPublicationAdapter

    def relation() -> dict[str, Any]:
        return {
            "id": 99,
            "type": "blocks",
            "description": adapter._relation_marker(
                "blocked_by", ("plan-a", "node"), ("plan-a", "target")
            ),
            "_links": {
                "from": {"href": "/api/v3/work_packages/2"},
                "to": {"href": "/api/v3/work_packages/1"},
            },
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal write_attempts
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([source, target])
        if request.url.path == "/api/v3/work_packages/1":
            return httpx.Response(200, json=source)
        if request.url.path == "/api/v3/work_packages/2":
            return httpx.Response(200, json=target)
        if request.url.path == "/api/v3/relations":
            return _collection(relations)
        if request.url.path == "/api/v3/work_packages/2/relations" and request.method == "POST":
            write_attempts += 1
            if persisted:
                relations.append(relation())
            raise httpx.ReadTimeout("lost response", request=request)
        if request.url.path == "/api/v3/relations/99" and request.method == "DELETE":
            write_attempts += 1
            if persisted:
                relations.clear()
            raise httpx.ReadTimeout("lost response", request=request)
        raise AssertionError(request.url)

    adapter = _adapter(httpx.MockTransport(handler))
    if kind == "remove_managed_relation":
        relations.append(relation())
    operation = PublicationOperation(
        operation_id="relation-loss",
        kind=kind,  # type: ignore[arg-type]
        identity=("plan-a", "node"),
        preconditions=_topology(
            relations=(
                (("blocked_by", ("plan-a", "target")),) if kind == "remove_managed_relation" else ()
            )
        ),
        managed_before_hash=source["customField43"],
        managed_after_hash=source["customField43"],
        trace_id="trace",
        payload={"type": "blocked_by", "target_identity": ("plan-a", "target")},
    )
    if persisted:
        adapter.apply(operation, idempotency_key="relation-loss", current=None)
        assert adapter.effects[-1].outcome == "applied"
    else:
        with pytest.raises(AmbiguousPublicationEffect, match="response was lost"):
            adapter.apply(operation, idempotency_key="relation-loss", current=None)
    assert write_attempts == 1


def test_human_relation_collision_stops_without_deletion() -> None:
    source = _raw(1)
    target = _raw(2, node="target")
    deleted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deleted
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([source, target])
        if request.url.path == "/api/v3/work_packages/1":
            return httpx.Response(200, json=source)
        if request.url.path == "/api/v3/work_packages/2":
            return httpx.Response(200, json=target)
        if request.url.path == "/api/v3/relations":
            return _collection(
                [
                    {
                        "id": 4,
                        "description": "human relation",
                        "_links": {
                            "from": {"href": "/api/v3/work_packages/2"},
                            "to": {"href": "/api/v3/work_packages/1"},
                        },
                        "type": "blocks",
                    }
                ]
            )
        if request.method == "DELETE":
            deleted = True
        raise AssertionError(request.url)

    operation = PublicationOperation(
        operation_id="relation-2",
        kind="create_relation",
        identity=("plan-a", "node"),
        preconditions=_topology(),
        managed_before_hash=source["customField43"],
        managed_after_hash=source["customField43"],
        trace_id="trace",
        payload={"type": "blocked_by", "target_identity": ("plan-a", "target")},
    )
    adapter = _adapter(httpx.MockTransport(handler))
    with pytest.raises(
        OpenProjectPublicationError, match="human/native relation semantic collision"
    ):
        adapter.apply(operation, idempotency_key="relation-2", current=None)
    assert not deleted


def test_supersede_sets_configured_status_and_never_issues_delete() -> None:
    raw = _raw(1)
    writes: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([raw])
        if request.url.path == "/api/v3/work_packages/1" and request.method == "GET":
            return httpx.Response(200, json=raw)
        if request.url.path.endswith("/form"):
            return _form(
                json.loads(request.content), href="/api/v3/work_packages/1", method="patch"
            )
        if request.url.path == "/api/v3/work_packages/1" and request.method == "PATCH":
            writes.append(json.loads(request.content))
            raw["_links"]["status"] = {"href": "/api/v3/statuses/29"}
            return httpx.Response(200, json=raw)
        if request.method == "DELETE":
            raise AssertionError("supersede must not delete a work package")
        raise AssertionError(request.url)

    operation = PublicationOperation(
        operation_id="supersede-1",
        kind="mark_superseded",
        identity=("plan-a", "node"),
        preconditions=_topology(),
        managed_before_hash=raw["customField43"],
        managed_after_hash=raw["customField43"],
        trace_id="trace",
        payload={"superseded": True},
    )
    adapter = _adapter(httpx.MockTransport(handler))
    adapter.apply(
        operation,
        idempotency_key="supersede-1",
        current=adapter.resolve(("plan-a", "node")),
    )
    assert writes == [{"_links": {"status": {"href": "/api/v3/statuses/29"}}, "lockVersion": 3}]


def test_success_response_without_supersede_postcondition_is_rejected() -> None:
    raw = _raw(1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([raw])
        if request.url.path == "/api/v3/work_packages/1" and request.method == "GET":
            return httpx.Response(200, json=raw)
        if request.url.path.endswith("/form"):
            return _form(
                json.loads(request.content),
                href="/api/v3/work_packages/1",
                method="patch",
            )
        if request.url.path == "/api/v3/work_packages/1" and request.method == "PATCH":
            return httpx.Response(200, json=raw)
        raise AssertionError(request.url)

    operation = PublicationOperation(
        operation_id="supersede-missing",
        kind="mark_superseded",
        identity=("plan-a", "node"),
        preconditions=_topology(),
        managed_before_hash=raw["customField43"],
        managed_after_hash=raw["customField43"],
        trace_id="trace",
        payload={"superseded": True},
    )
    adapter = _adapter(httpx.MockTransport(handler))
    with pytest.raises(AmbiguousPublicationEffect, match="exact postcondition"):
        adapter.apply(
            operation,
            idempotency_key=operation.operation_id,
            current=adapter.resolve(operation.identity),
        )


def test_parent_removal_postcondition_rejects_unmanaged_parent() -> None:
    child = _raw(1)
    child["_links"]["parent"] = {"href": "/api/v3/work_packages/9"}
    unmanaged_parent = {
        "id": 9,
        "lockVersion": 1,
        "subject": "Human-owned parent",
        "description": {"raw": "human"},
        "_links": {"status": {"href": "/api/v3/statuses/25"}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([child])
        if request.url.path == "/api/v3/work_packages/1":
            return httpx.Response(200, json=child)
        if request.url.path == "/api/v3/work_packages/9":
            return httpx.Response(200, json=unmanaged_parent)
        if request.url.path == "/api/v3/relations":
            return _collection([])
        raise AssertionError(request.url)

    operation = PublicationOperation(
        operation_id="remove-parent",
        kind="set_parent",
        identity=("plan-a", "node"),
        preconditions=_topology(unmanaged_parent=True),
        managed_before_hash=child["customField43"],
        managed_after_hash=child["customField43"],
        trace_id="trace",
        payload={"parent_identity": None},
    )
    assert not _adapter(httpx.MockTransport(handler)).postcondition(operation)


def test_managed_update_postcondition_rejects_concurrent_topology_drift() -> None:
    child = _raw(1, title="Generated title", generated=_AFTER_GENERATED)
    child["_links"]["parent"] = {"href": "/api/v3/work_packages/2"}
    parent = _raw(2, node="parent")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([child, parent])
        if request.url.path == "/api/v3/work_packages/1":
            return httpx.Response(200, json=child)
        if request.url.path == "/api/v3/work_packages/2":
            return httpx.Response(200, json=parent)
        if request.url.path == "/api/v3/relations":
            return _collection([])
        raise AssertionError(request.url)

    assert not _adapter(httpx.MockTransport(handler)).postcondition(_operation())


def test_reactivation_sets_exact_ready_status_and_converges() -> None:
    raw = _raw(1)
    raw["_links"]["status"] = {"href": "/api/v3/statuses/29"}
    writes: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects/42/work_packages"):
            return _collection([raw])
        if request.url.path == "/api/v3/work_packages/1" and request.method == "GET":
            return httpx.Response(200, json=raw)
        if request.url.path.endswith("/form"):
            return _form(
                json.loads(request.content),
                href="/api/v3/work_packages/1",
                method="patch",
            )
        if request.url.path == "/api/v3/work_packages/1" and request.method == "PATCH":
            payload = json.loads(request.content)
            writes.append(payload)
            raw["_links"]["status"] = {"href": "/api/v3/statuses/24"}
            raw["lockVersion"] = 4
            return httpx.Response(200, json=raw)
        raise AssertionError(request.url)

    operation = PublicationOperation(
        operation_id="reactivate-1",
        kind="reactivate_work_package",
        identity=("plan-a", "node"),
        preconditions=_topology(),
        managed_before_hash=raw["customField43"],
        managed_after_hash=raw["customField43"],
        trace_id="trace",
        payload={"status": "Ready"},
    )
    adapter = _adapter(httpx.MockTransport(handler))
    adapter.apply(
        operation,
        idempotency_key=operation.operation_id,
        current=adapter.resolve(operation.identity),
    )
    assert writes == [
        {
            "_links": {"status": {"href": "/api/v3/statuses/24"}},
            "lockVersion": 3,
        }
    ]
    assert adapter.postcondition(operation)


def test_pinned_bootstrap_uses_v176_models_and_exact_webhook_events() -> None:
    bootstrap = Path("openproject/bootstrap/17.6.0/bootstrap.rb").read_text()
    assert "OpenProject::VERSION.to_semver" in bootstrap
    assert (
        "ProjectRole" in bootstrap and "Workflow" in bootstrap and "Webhooks::Webhook" in bootstrap
    )
    assert "work_package_comment:comment" in bootstrap
    assert "work_package_comment:internal_comment" in bootstrap
    assert "work_package:comment_created" not in bootstrap
    assert "WorkflowTransition" not in bootstrap
    assert "templated: true" in bootstrap
    assert "default_done_ratio" in bootstrap
    assert "IssuePriority.where" in bootstrap
    assert "workflow.author = false" in bootstrap
    assert "role.permissions = required_permissions" in bootstrap
    assert "return rows.first if rows.one?" in bootstrap


def _observed_raw(adapter: OpenProjectPublicationAdapter) -> dict[str, Any]:
    raw = _raw(1)
    raw.update(
        {
            "subject": "Generated title",
            "description": {
                "raw": "Human preamble\n\n<!-- planning-platform:generated -->\nmanaged\n<!-- /planning-platform:generated -->\n\nHuman trailer"
            },
            "customField44": "example/repo",
            "customField45": "low",
            "customField46": True,
            "customField47": {
                "format": "markdown",
                "raw": '["REQ-1"]',
                "html": "<p>[&quot;REQ-1&quot;]</p>",
            },
            "customField48": "a" * 40,
            "_links": {
                "type": {"href": "/api/v3/types/14"},
                "priority": {"href": "/api/v3/priorities/50"},
                "status": {"href": "/api/v3/statuses/25"},
                "assignee": {"href": "/api/v3/users/9"},
            },
        }
    )
    raw["customField43"] = canonical_hash(adapter._observed_projection(raw))
    return raw


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.__setitem__("subject", "manual title"),
        lambda raw: raw["_links"].__setitem__("type", {"href": "/api/v3/types/13"}),
        lambda raw: raw.__setitem__(
            "description",
            {
                "raw": "<!-- planning-platform:generated -->\nchanged\n<!-- /planning-platform:generated -->"
            },
        ),
        lambda raw: raw["_links"].__setitem__("priority", {"href": "/api/v3/priorities/51"}),
        lambda raw: raw.__setitem__("customField45", "medium"),
        lambda raw: raw.__setitem__("customField44", "other/repo"),
        lambda raw: raw.__setitem__("customField47", "REQ-2"),
        lambda raw: raw.__setitem__("customField46", False),
        lambda raw: raw.__setitem__("customField48", "b" * 40),
        lambda raw: raw.__setitem__("customField40", "other-plan"),
        lambda raw: raw.__setitem__("customField41", "other-node"),
        lambda raw: raw.__setitem__("customField42", 2),
    ],
)
def test_observed_managed_field_tampering_rejects_old_hash_without_mutation(mutate) -> None:
    adapter = _adapter(
        httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError(request)))
    )
    raw = _observed_raw(adapter)
    mutate(raw)
    with pytest.raises(
        OpenProjectPublicationError,
        match=r"Managed hash|priority and Risk|Source requirements",
    ):
        adapter._work_package(raw)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["_links"].__setitem__("type", {"href": "/api/v3/types/999"}),
        lambda raw: raw.__setitem__("customField47", ["not-a-string"]),
        lambda raw: raw.__setitem__(
            "description", {"raw": "<!-- planning-platform:generated --> orphan"}
        ),
        lambda raw: raw.__setitem__("customField45", "medium"),
    ],
)
def test_observed_invalid_v3_shapes_fail_closed(mutate) -> None:
    adapter = _adapter(
        httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError(request)))
    )
    raw = _observed_raw(adapter)
    mutate(raw)
    with pytest.raises(OpenProjectPublicationError):
        adapter._work_package(raw)


@pytest.mark.parametrize("managed_hash", [None, "", "short"])
def test_managed_identity_without_valid_hash_fails_closed(
    managed_hash: str | None,
) -> None:
    adapter = _adapter(
        httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError(request)))
    )
    raw = _raw(1)
    raw["customField43"] = managed_hash
    with pytest.raises(OpenProjectPublicationError, match="valid Managed hash"):
        adapter._work_package(raw)


def test_human_owned_description_and_runtime_links_do_not_affect_observed_hash() -> None:
    adapter = _adapter(
        httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError(request)))
    )
    raw = _observed_raw(adapter)
    changed = {
        **raw,
        "description": {
            "raw": "Edited human preamble\n\n<!-- planning-platform:generated -->\nmanaged\n<!-- /planning-platform:generated -->\n\nEdited human trailer"
        },
    }
    changed["_links"] = {
        **raw["_links"],
        "assignee": {"href": "/api/v3/users/77"},
        "status": {"href": "/api/v3/statuses/28"},
    }
    assert canonical_hash(adapter._observed_projection(changed)) == raw["customField43"]
    adapter._work_package(changed)


def test_http_publication_parent_relation_roundtrip_replays_to_zero_diff(tmp_path: Path) -> None:
    """The fake server persists only form/commit payloads, then is read afresh."""
    artifact = load_artifact(Path("evals/fixtures/single-repository/backlog.yaml"))
    child = artifact.plan.items[0].model_copy(update={"parent": "epic", "blocked_by": ("epic",)})
    epic = child.model_copy(
        update={
            "key": "epic",
            "type": "Epic",
            "title": "Parent epic",
            "parent": None,
            "blocked_by": (),
        }
    )
    plan = artifact.plan.model_copy(update={"items": (child, epic)})
    artifact_path = tmp_path / "backlog.yaml"
    artifact_path.write_text(yaml.safe_dump(plan.model_dump(mode="json"), sort_keys=False))
    artifact = load_artifact(artifact_path)
    plan = artifact.plan
    base = plan.plan.openproject_snapshot
    envelope = PublicationEnvelope(
        "a" * 40,
        artifact.sha256,
        artifact.blob_sha1,
        "roundtrip",
        base.sha256,
        base.etag,
        "trace",
        plan.plan.publication_identity,
    )
    state: dict[int, dict[str, Any]] = {}
    relations: list[dict[str, Any]] = []

    def work_package(identifier: int, payload: dict[str, Any]) -> dict[str, Any]:
        links = payload.get("_links", {})
        return {
            "id": identifier,
            "lockVersion": payload.get("lockVersion", 0),
            "subject": payload.get("subject", ""),
            "description": payload.get("description", {"raw": ""}),
            **{key: value for key, value in payload.items() if key.startswith("customField")},
            "_links": {**links, "status": links.get("status", {"href": "/api/v3/statuses/25"})},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/projects/42/work_packages"):
            return _collection(list(state.values()))
        if path == "/api/v3/relations":
            return _collection(relations)
        if path.startswith("/api/v3/work_packages/") and request.method == "GET":
            return httpx.Response(200, json=state[int(path.rsplit("/", 1)[-1])])
        if path.endswith("/form"):
            target = (
                path.removesuffix("/form")
                if path != "/api/v3/work_packages/form"
                else "/api/v3/work_packages"
            )
            method = "patch" if target != "/api/v3/work_packages" else "post"
            return _form(json.loads(request.content), href=target, method=method)
        if path == "/api/v3/work_packages" and request.method == "POST":
            identifier = len(state) + 1
            state[identifier] = work_package(identifier, json.loads(request.content))
            return httpx.Response(201, json=state[identifier])
        if path.startswith("/api/v3/work_packages/") and request.method == "PATCH":
            identifier = int(path.rsplit("/", 1)[-1])
            payload = json.loads(request.content)
            state[identifier] = work_package(
                identifier,
                {
                    **state[identifier],
                    **payload,
                    "_links": {**state[identifier]["_links"], **payload.get("_links", {})},
                },
            ) | {"lockVersion": 1}
            return httpx.Response(200, json=state[identifier])
        if path.endswith("/relations") and request.method == "POST":
            source = int(path.split("/")[4])
            body = json.loads(request.content)
            target = int(body["_links"]["to"]["href"].rsplit("/", 1)[-1])
            relations.append(
                {
                    "id": 1,
                    "type": body["type"],
                    "description": body["description"],
                    "_links": {
                        "from": {"href": f"/api/v3/work_packages/{source}"},
                        "to": {"href": f"/api/v3/work_packages/{target}"},
                    },
                }
            )
            return httpx.Response(201, json=relations[-1])
        raise AssertionError(request.url)

    class FirstAdapter(OpenProjectPublicationAdapter):
        def snapshot(self):
            from planning_platform.openproject import OpenProjectSnapshot

            return OpenProjectSnapshot("now", base.etag, base.sha256, ())

    first = FirstAdapter(
        _config(),
        "test",
        client=httpx.Client(base_url="https://op.test", transport=httpx.MockTransport(handler)),
    )
    publish(artifact, first, envelope, apply=True, journal=InMemoryPublicationJournal())
    fresh = OpenProjectPublicationAdapter(
        _config(),
        "test",
        client=httpx.Client(base_url="https://op.test", transport=httpx.MockTransport(handler)),
    )
    snapshot = fresh.snapshot()
    assert (
        plan_diff(
            plan.model_copy(
                update={"plan": plan.plan.model_copy(update={"approved_planning_commit": "a" * 40})}
            ),
            snapshot,
        )
        == ()
    )


def test_lifecycle_status_evidence_and_comment_updates_are_form_first_and_replay_safe() -> None:
    current = _raw(7)
    activities: list[dict[str, Any]] = []
    writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal current, writes
        path = request.url.path
        if path == "/api/v3/work_packages/7" and request.method == "GET":
            return httpx.Response(200, json=current)
        if path == "/api/v3/work_packages/7/form":
            payload = json.loads(request.content)
            assert payload["lockVersion"] == current["lockVersion"]
            return _form(
                payload,
                href="/api/v3/work_packages/7",
                method="patch",
            )
        if path == "/api/v3/work_packages/7" and request.method == "PATCH":
            writes += 1
            payload = json.loads(request.content)
            current = {
                **current,
                **{
                    key: value
                    for key, value in payload.items()
                    if key.startswith("customField")
                },
                "lockVersion": current["lockVersion"] + 1,
                "_links": {
                    **current["_links"],
                    **payload.get("_links", {}),
                },
            }
            return httpx.Response(200, json=current)
        if path == "/api/v3/work_packages/7/activities" and request.method == "GET":
            return _collection(activities)
        if path == "/api/v3/work_packages/7/activities" and request.method == "POST":
            writes += 1
            activity = {
                "id": 101,
                "comment": json.loads(request.content)["comment"],
            }
            activities.append(activity)
            return httpx.Response(201, json=activity)
        raise AssertionError(request)

    adapter = _adapter(httpx.MockTransport(handler))
    updated = adapter.set_lifecycle_state(
        7,
        status="Review",
        evidence_state="pr_merged",
    )
    assert updated["_links"]["status"]["href"] == "/api/v3/statuses/27"
    assert updated["customField49"] == "pr_merged"
    assert adapter.set_lifecycle_state(
        7,
        status="Review",
        evidence_state="pr_merged",
    ) == updated
    assert adapter.ensure_comment(
        7,
        "Implementation evidence received.",
        idempotency_key="comment:test:00000001",
    ) == 101
    assert adapter.ensure_comment(
        7,
        "Implementation evidence received.",
        idempotency_key="comment:test:00000001",
    ) == 101
    assert writes == 2
