from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ValidationError

from planning_platform.loader import load_artifact, load_artifact_bytes
from planning_platform.planner.api import create_app
from planning_platform.planner.execution import (
    ExecutionInProgress,
    InMemoryExecutionGuard,
    _close_connection,
)
from planning_platform.planner.graph import build_planner_graph
from planning_platform.planner.idempotency import (
    IdempotencyClaim,
    IdempotencyConflict,
    IdempotencyInProgress,
    InMemoryIdempotencyRepository,
)
from planning_platform.planner.model import DeterministicPlanningModel
from planning_platform.planner.models import (
    MAX_PLANNER_REQUEST_BYTES,
    ConsequentialQuestions,
    DecompositionCritique,
    DecompositionDraft,
    DocumentSection,
    IdeaSnapshot,
    OpenProjectSnapshotInput,
    RelationDraft,
    RepositoryFile,
    RequirementsDraft,
    ResumePlanRequest,
    StartPlanRequest,
    idea_snapshot_digest,
    repository_snapshot_digest,
)
from planning_platform.planner.service import PlannerService
from planning_platform.replan import apply_replan_boundary, validate_replan_candidate
from planning_platform.validation import SemanticValidationError

INTERNAL_TOKEN = "internal-test-token"
AUTH_HEADERS = {"X-Planning-Internal-Token": INTERNAL_TOKEN}


def after_timestamp(value: str | datetime, *, seconds: int = 1) -> str:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    return (parsed + timedelta(seconds=seconds)).isoformat()


def start_payload(
    *,
    idea_id: int = 41,
    description: str = "Implement a bounded change.",
    idempotency_key: str = "start:fixture:0001",
) -> dict[str, Any]:
    content = "def existing() -> str:\n    return 'safe repository context'\n"
    repository_file = RepositoryFile(
        path="src/existing.py",
        sha256=hashlib.sha256(content.encode()).hexdigest(),
        content=content,
    )
    idea = {
        "work_package_id": idea_id,
        "lock_version": 2,
        "updated_at": "2026-07-30T00:00:00Z",
        "title": "Bounded feature",
        "description": description,
    }
    snapshot = {
        "captured_at": "2026-07-30T00:00:00Z",
        "etag": "fixture-etag",
        "sha256": "b" * 64,
    }
    payload = {
        "event": {
            "idempotency_key": idempotency_key,
            "trace_id": str(uuid4()),
        },
        "idea": idea,
        "plan_id": f"fixture-plan-{idea_id}",
        "plan_version": 1,
        "idea_sha256": idea_snapshot_digest(
            IdeaSnapshot.model_validate(idea),
            OpenProjectSnapshotInput.model_validate(snapshot),
        ),
        "openproject_snapshot": snapshot,
        "repositories": [
            {
                "name": "Acme/service",
                "commit": "a" * 40,
                "snapshot_sha256": repository_snapshot_digest(
                    "Acme/service", "a" * 40, (repository_file,)
                ),
                "files": [repository_file.model_dump(mode="json")],
            }
        ],
    }
    return payload


def test_replan_request_and_backlog_validation_are_closed_over_selected_roots() -> None:
    prior = load_artifact(
        Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    ).plan
    root = prior.items[0]
    protected = root.model_copy(update={"key": "protected-node"})
    prior = prior.model_copy(update={"items": (root, protected)})
    payload = start_payload(idea_id=1)
    payload.update({"plan_id": prior.plan.id, "plan_version": 2})
    payload["replan"] = {
        "prior_plan": prior.model_dump(mode="json"),
        "base_approved_planning_commit": "c" * 40,
        "selected_root_keys": [root.key],
        "affected_node_keys": [root.key],
        "reason": "Refine only the selected branch.",
    }
    request = StartPlanRequest.model_validate(payload)
    assert request.replan is not None
    with pytest.raises(ValidationError, match="descendant closure"):
        StartPlanRequest.model_validate(
            {**payload, "replan": {**payload["replan"], "affected_node_keys": ["protected-node"]}}
        )

    changed_protected = protected.model_copy(update={"title": "Changed outside scope"})
    child = root.model_copy(update={"key": "new-selected-child", "parent": root.key})
    proposed = prior.model_copy(
        update={
            "plan": prior.plan.model_copy(
                update={
                    "version": 2,
                    "publication_identity": f"{prior.plan.id}:v2",
                }
            ),
            "items": (root, changed_protected, child),
        }
    )
    bounded = apply_replan_boundary(
        prior,
        proposed,
        base_approved_commit="c" * 40,
        selected_root_keys=(root.key,),
        affected_node_keys=(root.key,),
    )
    assert bounded.by_key[protected.key] == protected
    assert bounded.plan.replan is not None
    assert bounded.plan.replan.retained_node_bindings[0].node_key == protected.key

    escaped = bounded.model_copy(
        update={
            "items": tuple(
                item.model_copy(update={"parent": protected.key}) if item.key == child.key else item
                for item in bounded.items
            )
        }
    )
    with pytest.raises(ValueError, match="not rooted"):
        validate_replan_candidate(
            prior,
            escaped,
            base_approved_commit="c" * 40,
            selected_root_keys=(root.key,),
            affected_node_keys=(root.key,),
        )


def test_replan_preserves_each_existing_nodes_selected_root() -> None:
    loaded = load_artifact(
        Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    ).plan
    seed = loaded.items[0]
    root_a = seed.model_copy(update={"key": "root-a", "type": "Epic", "parent": None})
    child_a = seed.model_copy(
        update={"key": "child-a", "type": "Story", "parent": root_a.key}
    )
    root_b = seed.model_copy(update={"key": "root-b", "type": "Epic", "parent": None})
    prior = loaded.model_copy(update={"items": (root_a, child_a, root_b)})
    proposal = prior.model_copy(
        update={
            "plan": prior.plan.model_copy(
                update={
                    "version": 2,
                    "publication_identity": f"{prior.plan.id}:v2",
                }
            )
        }
    )
    bounded = apply_replan_boundary(
        prior,
        proposal,
        base_approved_commit="c" * 40,
        selected_root_keys=(root_a.key, root_b.key),
        affected_node_keys=(root_a.key, child_a.key, root_b.key),
    )
    escaped = bounded.model_copy(
        update={
            "items": tuple(
                item.model_copy(update={"parent": root_b.key})
                if item.key == child_a.key
                else item
                for item in bounded.items
            )
        }
    )

    with pytest.raises(ValueError, match="changed selected-root ownership"):
        validate_replan_candidate(
            prior,
            escaped,
            base_approved_commit="c" * 40,
            selected_root_keys=(root_a.key, root_b.key),
            affected_node_keys=(root_a.key, child_a.key, root_b.key),
        )

    with pytest.raises(ValueError, match="nested selected roots"):
        apply_replan_boundary(
            prior,
            proposal,
            base_approved_commit="c" * 40,
            selected_root_keys=(root_a.key, child_a.key),
            affected_node_keys=(root_a.key, child_a.key),
        )


@pytest.mark.asyncio
async def test_deterministic_replan_artifact_preserves_protected_nodes_and_bindings() -> None:
    prior = load_artifact(
        Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    ).plan
    root = prior.items[0]
    protected = root.model_copy(update={"key": "protected-node", "title": "Protected node"})
    prior = prior.model_copy(update={"items": (root, protected)})
    payload = start_payload(idea_id=prior.plan.source_idea.work_package_id)
    payload.update({"plan_id": prior.plan.id, "plan_version": 2})
    payload["replan"] = {
        "prior_plan": prior.model_dump(mode="json"),
        "base_approved_planning_commit": "c" * 40,
        "selected_root_keys": [root.key],
        "affected_node_keys": [root.key],
        "reason": "Refine only the selected root.",
    }
    service, _, _, _ = make_service()

    response, _ = await service.start(StartPlanRequest.model_validate(payload))
    bundle = await service.artifacts(response.thread_id)
    content = next(item.content for item in bundle.artifacts if item.path == "backlog.yaml")
    artifact = load_artifact_bytes(content.encode())

    assert artifact.plan.by_key[protected.key] == protected
    assert artifact.plan.plan.replan is not None
    assert artifact.plan.plan.replan.retained_node_bindings[0].node_key == protected.key
    assert artifact.plan.plan.replan.retained_node_bindings[0].planning_commit == "c" * 40


def make_service(
    *,
    checkpointer: InMemorySaver | None = None,
    idempotency: InMemoryIdempotencyRepository | None = None,
    model: Any = None,
    execution_guard: Any = None,
) -> tuple[PlannerService, InMemorySaver, InMemoryIdempotencyRepository, Any]:
    saver = checkpointer or InMemorySaver()
    repository = idempotency or InMemoryIdempotencyRepository()
    graph = build_planner_graph(model or DeterministicPlanningModel(), saver)
    guard = execution_guard or InMemoryExecutionGuard(graph)
    return PlannerService(graph, repository, guard), saver, repository, graph


async def assert_checkpoint_history_excludes(
    graph: Any,
    thread_id: str,
    *forbidden_values: str,
) -> None:
    config = {"configurable": {"thread_id": thread_id}}
    async for snapshot in graph.aget_state_history(config):
        persisted = repr(snapshot)
        for forbidden in forbidden_values:
            assert forbidden not in persisted


@pytest.mark.asyncio
async def test_health_metrics_start_get_replay_and_stable_identity() -> None:
    service, _, _, _ = make_service()
    app = create_app(service, internal_token=INTERNAL_TOKEN)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=AUTH_HEADERS,
    ) as client:
        assert (
            await client.get(
                "/health/live",
                headers={"X-Planning-Internal-Token": "wrong"},
            )
        ).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200
        metrics = await client.get("/metrics")
        assert metrics.status_code == 200
        assert "planning_platform_plans_started_total" in metrics.text

        payload = start_payload()
        assert (
            await client.post(
                "/v1/plans",
                json=payload,
                headers={"X-Planning-Internal-Token": "wrong"},
            )
        ).status_code == 401
        created = await client.post("/v1/plans", json=payload)
        assert created.status_code == 201
        body = created.json()
        assert body["thread_id"] == "openproject:41:planning:1"
        assert body["status"] == "artifacts_ready"
        assert {entry["path"] for entry in body["artifact_manifest"]} == {
            "SPEC.md",
            "ARCHITECTURE.md",
            "DECISIONS.md",
            "backlog.yaml",
            "backlog.mmd",
            "VALIDATION.md",
        }
        path = f"/v1/plans/{body['thread_id']}"
        assert (
            await client.get(
                path,
                headers={"X-Planning-Internal-Token": "wrong"},
            )
        ).status_code == 401
        fetched = await client.get(path)
        assert fetched.json() == body
        replay = await client.post("/v1/plans", json=payload)
        assert replay.status_code == 200
        assert replay.json() == body
        assert (await client.get("/v1/plans/arbitrary")).status_code == 422


@pytest.mark.asyncio
async def test_authenticated_artifact_handoff_returns_exact_verified_bytes() -> None:
    service, _, _, _ = make_service()
    app = create_app(service, internal_token=INTERNAL_TOKEN)
    headers = AUTH_HEADERS
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        pending = await client.post(
            "/v1/plans",
            json=start_payload(idea_id=57, description="Choose? artifact handoff"),
            headers=headers,
        )
        pending_path = f"/v1/plans/{pending.json()['thread_id']}/artifacts"
        assert (await client.get(pending_path)).status_code == 401
        assert (
            await client.get(
                pending_path,
                headers={"X-Planning-Internal-Token": "wrong"},
            )
        ).status_code == 401
        assert (await client.get(pending_path, headers=headers)).status_code == 409

        ready = await client.post(
            "/v1/plans",
            json=start_payload(
                idea_id=58,
                idempotency_key="start:fixture:artifact-ready",
            ),
            headers=headers,
        )
        path = f"/v1/plans/{ready.json()['thread_id']}/artifacts"
        response = await client.get(path, headers=headers)
        assert response.status_code == 200
        artifacts = response.json()["artifacts"]
        assert {artifact["path"] for artifact in artifacts} == {
            entry["path"] for entry in ready.json()["artifact_manifest"]
        }
        for artifact in artifacts:
            assert artifact["sha256"] == hashlib.sha256(artifact["content"].encode()).hexdigest()
        spec = next(artifact["content"] for artifact in artifacts if artifact["path"] == "SPEC.md")
        assert "## Requirements\n- REQ-1" in spec


@pytest.mark.asyncio
async def test_changed_start_body_and_repository_hash_are_rejected() -> None:
    service, _, _, _ = make_service()
    app = create_app(service, internal_token=INTERNAL_TOKEN)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=AUTH_HEADERS,
    ) as client:
        payload = start_payload()
        assert (await client.post("/v1/plans", json=payload)).status_code == 201
        changed = deepcopy(payload)
        changed["idea"]["title"] = "Changed title"
        changed["idea_sha256"] = idea_snapshot_digest(
            IdeaSnapshot.model_validate(changed["idea"]),
            OpenProjectSnapshotInput.model_validate(changed["openproject_snapshot"]),
        )
        assert (await client.post("/v1/plans", json=changed)).status_code == 409
        invalid = start_payload(idea_id=42)
        invalid["repositories"][0]["files"][0]["sha256"] = "c" * 64
        assert (await client.post("/v1/plans", json=invalid)).status_code == 422


@pytest.mark.asyncio
async def test_naive_intake_dates_return_422() -> None:
    service, _, _, _ = make_service()
    app = create_app(service, internal_token=INTERNAL_TOKEN)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=AUTH_HEADERS,
    ) as client:
        for location, field in (
            ("idea", "updated_at"),
            ("openproject_snapshot", "captured_at"),
        ):
            payload = start_payload(idea_id=64)
            payload[location][field] = "2026-07-30T00:00:00"
            response = await client.post("/v1/plans", json=payload)
            assert response.status_code == 422


@pytest.mark.asyncio
async def test_interrupt_resume_replay_and_comment_binding() -> None:
    service, _, _, graph = make_service()
    app = create_app(service, internal_token=INTERNAL_TOKEN)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=AUTH_HEADERS,
    ) as client:
        start = await client.post(
            "/v1/plans",
            json=start_payload(idea_id=43, description="Choose? a consequential option"),
        )
        assert start.status_code == 201
        pending = start.json()
        assert pending["status"] == "needs_input"
        assert pending["interrupt"]["impact"]
        assert (
            datetime.fromisoformat(pending["interrupt"]["created_at"].replace("Z", "+00:00")).tzinfo
            is not None
        )
        interrupt_id = pending["interrupt"]["interrupt_id"]
        resume_trace_id = str(uuid4())
        resume = {
            "event": {
                "idempotency_key": "resume:fixture:0001",
                "trace_id": resume_trace_id,
            },
            "interrupt_id": interrupt_id,
            "comment_id": 9001,
            "comment_created_at": after_timestamp(pending["interrupt"]["created_at"]),
            "answer": "Use option A.",
        }
        wrong = deepcopy(resume)
        wrong["event"]["idempotency_key"] = "resume:wrong:0001"
        wrong["interrupt_id"] = "wrong-interrupt"
        path = f"/v1/plans/{pending['thread_id']}/resume"
        assert (
            await client.post(
                path,
                json=resume,
                headers={"X-Planning-Internal-Token": "wrong"},
            )
        ).status_code == 401
        assert (await client.post(path, json=wrong)).status_code == 409
        stale = deepcopy(resume)
        stale["event"]["idempotency_key"] = "resume:stale:0001"
        stale["comment_created_at"] = pending["interrupt"]["created_at"]
        assert (await client.post(path, json=stale)).status_code == 409

        completed = await client.post(path, json=resume)
        assert completed.status_code == 200
        assert completed.json()["status"] == "artifacts_ready"
        assert completed.json()["trace_id"] == resume_trace_id
        checkpoint = await graph.aget_state({"configurable": {"thread_id": pending["thread_id"]}})
        assert checkpoint.values["trace_id"] == resume_trace_id
        replay = await client.post(path, json=resume)
        assert replay.json() == completed.json()
        assert replay.json()["trace_id"] == resume_trace_id

        changed = deepcopy(resume)
        changed["answer"] = "Changed replay body."
        assert (await client.post(path, json=changed)).status_code == 409
        rebound = deepcopy(resume)
        rebound["event"]["idempotency_key"] = "resume:rebind:0001"
        rebound["answer"] = "Another answer."
        assert (await client.post(path, json=rebound)).status_code == 409


@pytest.mark.asyncio
async def test_resume_repairs_an_unacceptable_decomposition_critique() -> None:
    class RejectThenAcceptCritic(DeterministicPlanningModel):
        critique_calls = 0

        async def generate(self, stage, schema, payload):
            if schema is DecompositionDraft and stage == "revise_decomposition":
                draft = cast(
                    DecompositionDraft,
                    await super().generate(stage, schema, payload),
                )
                revised = draft.items[0].model_copy(
                    update={
                        "key": "implement-revised-request",
                        "title": "Implement the revised request",
                    }
                )
                return DecompositionDraft(items=(revised,))
            if schema is RelationDraft and payload.get("relation_draft"):
                return RelationDraft.model_validate(payload["relation_draft"])
            if schema is DecompositionCritique:
                self.critique_calls += 1
                if self.critique_calls == 1:
                    return DecompositionCritique(
                        acceptable=False,
                        findings=("Split the implementation into a narrower story.",),
                    )
                assert stage == "critique_decomposition"
                assert payload["critique"]["findings"] == [
                    "Split the implementation into a narrower story."
                ]
            return await super().generate(stage, schema, payload)

    model = RejectThenAcceptCritic()
    service, _, _, _ = make_service(model=model)
    app = create_app(service, internal_token=INTERNAL_TOKEN)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=AUTH_HEADERS,
    ) as client:
        started = await client.post(
            "/v1/plans",
            json=start_payload(
                idea_id=71,
                description="Choose? a material platform architecture.",
            ),
        )
        pending = started.json()
        assert pending["status"] == "needs_input"
        resumed = await client.post(
            f"/v1/plans/{pending['thread_id']}/resume",
            json={
                "event": {
                    "idempotency_key": "resume:critique-repair:0001",
                    "trace_id": str(uuid4()),
                },
                "interrupt_id": pending["interrupt"]["interrupt_id"],
                "comment_id": 9071,
                "comment_created_at": after_timestamp(
                    pending["interrupt"]["created_at"]
                ),
                "answer": "Use option 1.",
            },
        )
        artifacts = await client.get(
            f"/v1/plans/{pending['thread_id']}/artifacts"
        )

    assert resumed.status_code == 200
    assert resumed.json()["status"] == "artifacts_ready"
    assert model.critique_calls == 2
    backlog = next(
        artifact["content"]
        for artifact in artifacts.json()["artifacts"]
        if artifact["path"] == "backlog.yaml"
    )
    assert "key: implement-revised-request" in backlog
    assert "key: implement-request\n" not in backlog


@pytest.mark.asyncio
async def test_resume_returns_typed_failure_when_critique_repair_is_rejected() -> None:
    class AlwaysRejectingCritic(DeterministicPlanningModel):
        critique_calls = 0

        async def generate(self, stage, schema, payload):
            if schema is DecompositionCritique:
                self.critique_calls += 1
                return DecompositionCritique(
                    acceptable=False,
                    findings=("The proposed stories remain overbroad.",),
                )
            return await super().generate(stage, schema, payload)

    model = AlwaysRejectingCritic()
    service, _, _, _ = make_service(model=model)
    app = create_app(service, internal_token=INTERNAL_TOKEN)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=AUTH_HEADERS,
    ) as client:
        started = await client.post(
            "/v1/plans",
            json=start_payload(
                idea_id=72,
                description="Choose? a material platform architecture.",
            ),
        )
        pending = started.json()
        resumed = await client.post(
            f"/v1/plans/{pending['thread_id']}/resume",
            json={
                "event": {
                    "idempotency_key": "resume:critique-rejected:0001",
                    "trace_id": str(uuid4()),
                },
                "interrupt_id": pending["interrupt"]["interrupt_id"],
                "comment_id": 9072,
                "comment_created_at": after_timestamp(
                    pending["interrupt"]["created_at"]
                ),
                "answer": "Use option 1.",
            },
        )

    assert resumed.status_code == 200
    assert resumed.json()["status"] == "failed"
    assert model.critique_calls == 2


@pytest.mark.asyncio
async def test_reclaimed_resume_continues_from_checkpointed_critique_repair() -> None:
    class FailFinalCritiqueOnce(DeterministicPlanningModel):
        critique_calls = 0
        revision_calls = 0
        relation_calls = 0

        async def generate(self, stage, schema, payload):
            if schema is DecompositionDraft and stage == "revise_decomposition":
                self.revision_calls += 1
            if schema is RelationDraft:
                self.relation_calls += 1
            if schema is DecompositionCritique:
                self.critique_calls += 1
                if self.critique_calls == 1:
                    return DecompositionCritique(
                        acceptable=False,
                        findings=("Revise the story boundary.",),
                    )
                if self.critique_calls == 2:
                    raise RuntimeError("final critique timed out")
            return await super().generate(stage, schema, payload)

    model = FailFinalCritiqueOnce()
    repository = InMemoryIdempotencyRepository()
    service, saver, _, _ = make_service(idempotency=repository, model=model)
    app = create_app(service, internal_token=INTERNAL_TOKEN)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=AUTH_HEADERS,
    ) as client:
        started = await client.post(
            "/v1/plans",
            json=start_payload(
                idea_id=73,
                description="Choose? a material platform architecture.",
            ),
        )
        pending = started.json()
        resume = {
            "event": {
                "idempotency_key": "resume:critique-checkpoint:0001",
                "trace_id": str(uuid4()),
            },
            "interrupt_id": pending["interrupt"]["interrupt_id"],
            "comment_id": 9073,
            "comment_created_at": after_timestamp(
                pending["interrupt"]["created_at"]
            ),
            "answer": "Use option 1.",
        }
        failed = await client.post(
            f"/v1/plans/{pending['thread_id']}/resume",
            json=resume,
        )

    assert failed.status_code == 500
    repository.expire_for_test(resume["event"]["idempotency_key"])
    restarted, _, _, _ = make_service(
        checkpointer=saver,
        idempotency=repository,
        model=model,
    )
    restarted_app = create_app(restarted, internal_token=INTERNAL_TOKEN)
    async with AsyncClient(
        transport=ASGITransport(app=restarted_app),
        base_url="http://test",
        headers=AUTH_HEADERS,
    ) as client:
        recovered = await client.post(
            f"/v1/plans/{pending['thread_id']}/resume",
            json=resume,
        )

    assert recovered.status_code == 200
    assert recovered.json()["status"] == "artifacts_ready"
    assert model.critique_calls == 3
    assert model.revision_calls == 1
    assert model.relation_calls == 2


@pytest.mark.asyncio
async def test_shared_checkpointer_survives_service_restart() -> None:
    service, saver, repository, _ = make_service()
    request = StartPlanRequest.model_validate(
        start_payload(idea_id=44, description="Choose? restart behavior")
    )
    pending, _ = await service.start(request)
    assert pending.interrupt is not None

    restarted, _, _, _ = make_service(
        checkpointer=saver,
        idempotency=repository,
    )
    assert (await restarted.get(pending.thread_id)).status == "needs_input"
    resume = ResumePlanRequest.model_validate(
        {
            "event": {
                "idempotency_key": "resume:restart:001",
                "trace_id": str(uuid4()),
            },
            "interrupt_id": pending.interrupt.interrupt_id,
            "comment_id": 9010,
            "comment_created_at": after_timestamp(pending.interrupt.created_at),
            "answer": "Option A.",
        }
    )
    completed, _ = await restarted.resume(pending.thread_id, resume)
    assert completed.status == "artifacts_ready"


@pytest.mark.asyncio
async def test_material_plan_has_prd_and_checkpoint_excludes_raw_content() -> None:
    service, _, _, graph = make_service()
    payload = start_payload(
        idea_id=45,
        description="Migrate the platform architecture across multiple components.",
    )
    raw_secret = "token=TOP_SECRET_VALUE"
    payload["repositories"][0]["files"][0]["content"] = raw_secret
    payload["repositories"][0]["files"][0]["sha256"] = hashlib.sha256(
        raw_secret.encode()
    ).hexdigest()
    file = RepositoryFile.model_validate(payload["repositories"][0]["files"][0])
    payload["repositories"][0]["snapshot_sha256"] = repository_snapshot_digest(
        "Acme/service", "a" * 40, (file,)
    )
    response, _ = await service.start(StartPlanRequest.model_validate(payload))
    assert "PRD.md" in {entry.path for entry in response.artifact_manifest}
    await assert_checkpoint_history_excludes(
        graph,
        response.thread_id,
        "TOP_SECRET_VALUE",
    )


@pytest.mark.asyncio
async def test_idea_and_resume_secrets_never_enter_any_checkpoint_history() -> None:
    title_secret = "token=title-visible-secret"
    description_secret = "token=description-visible-secret"
    payload = start_payload(
        idea_id=69,
        description=f"Choose? {description_secret}",
    )
    payload["idea"]["title"] = title_secret
    payload["idea_sha256"] = idea_snapshot_digest(
        IdeaSnapshot.model_validate(payload["idea"]),
        OpenProjectSnapshotInput.model_validate(payload["openproject_snapshot"]),
    )
    service, _, _, graph = make_service()
    pending, _ = await service.start(StartPlanRequest.model_validate(payload))
    assert pending.interrupt is not None
    answer_secret = "token=answer-visible-secret"
    resume = ResumePlanRequest.model_validate(
        {
            "event": {
                "idempotency_key": "resume:history:0001",
                "trace_id": str(uuid4()),
            },
            "interrupt_id": pending.interrupt.interrupt_id,
            "comment_id": 9069,
            "comment_created_at": after_timestamp(pending.interrupt.created_at),
            "answer": answer_secret,
        }
    )
    completed, _ = await service.resume(pending.thread_id, resume)
    assert completed.status == "artifacts_ready"
    await assert_checkpoint_history_excludes(
        graph,
        pending.thread_id,
        title_secret,
        description_secret,
        answer_secret,
    )


@pytest.mark.asyncio
async def test_graph_has_required_stages_and_semantic_output_is_validated() -> None:
    class InvalidDraftModel(DeterministicPlanningModel):
        async def generate(
            self, stage: str, schema: type[BaseModel], payload: dict[str, Any]
        ) -> BaseModel:
            value = await super().generate(stage, schema, payload)
            if schema is RelationDraft:
                draft = cast(RelationDraft, value)
                invalid = draft.items[0].model_copy(update={"blocked_by": (draft.items[0].key,)})
                return RelationDraft(items=(invalid,))
            return value

    service, _, _, graph = make_service(model=InvalidDraftModel())
    assert {
        "normalize_intake",
        "classify_scope_and_risk",
        "retrieve_repository_context",
        "draft_compact_specification",
        "identify_consequential_questions",
        "human_interrupt_when_required",
        "generate_prd_when_warranted",
        "generate_architecture",
        "derive_requirements_and_decisions",
        "decompose_epics_and_stories",
        "infer_typed_relations",
        "critique_decomposition",
        "validate_backlog",
        "render_artifacts",
        "prepare_planning_pr",
    } <= set(graph.nodes)
    with pytest.raises(SemanticValidationError):
        await service.start(StartPlanRequest.model_validate(start_payload(idea_id=46)))


@pytest.mark.asyncio
async def test_decomposition_relation_and_independent_critic_inspect_real_drafts() -> None:
    seen: dict[str, dict[str, Any]] = {}

    class TransformingModel(DeterministicPlanningModel):
        async def generate(self, stage, schema, payload):
            seen[stage] = payload
            value = await super().generate(stage, schema, payload)
            if schema is DecompositionDraft:
                draft = cast(DecompositionDraft, value)
                verification = draft.items[0].model_copy(
                    update={
                        "key": "verify-request",
                        "title": "Verify decomposed request",
                    }
                )
                return DecompositionDraft(items=(*draft.items, verification))
            if schema is RelationDraft:
                draft = cast(RelationDraft, value)
                related = draft.items[1].model_copy(update={"blocked_by": (draft.items[0].key,)})
                return RelationDraft(items=(draft.items[0], related))
            return value

    service, _, _, graph = make_service(model=TransformingModel())
    response, _ = await service.start(StartPlanRequest.model_validate(start_payload(idea_id=55)))
    state = await graph.aget_state({"configurable": {"thread_id": response.thread_id}})
    assert len(state.values["backlog"]["items"]) == 2
    assert state.values["backlog"]["items"][1]["blocked_by"] == ["implement-request"]
    critic_payload = seen["critique_decomposition"]
    assert len(critic_payload["decomposition"]["items"]) == 2
    assert critic_payload["relation_draft"]["items"][1]["blocked_by"] == ["implement-request"]
    assert critic_payload["architecture"]


@pytest.mark.asyncio
async def test_independent_critic_rejects_overbroad_story() -> None:
    class OverbroadCritic(DeterministicPlanningModel):
        async def generate(self, stage, schema, payload):
            if schema is DecompositionDraft:
                value = cast(
                    DecompositionDraft,
                    await super().generate(stage, schema, payload),
                )
                overbroad = value.items[0].model_copy(
                    update={
                        "title": "Replace every platform subsystem",
                        "objective": ("Replace every platform subsystem in one indivisible story."),
                    }
                )
                return DecompositionDraft(items=(overbroad,))
            if schema is DecompositionCritique:
                assert (
                    "every platform subsystem"
                    in payload["relation_draft"]["items"][0]["title"].casefold()
                )
                return DecompositionCritique(
                    acceptable=False,
                    findings=("Story is overbroad and not vertically bounded.",),
                )
            return await super().generate(stage, schema, payload)

    service, _, _, _ = make_service(model=OverbroadCritic())
    response, _ = await service.start(
        StartPlanRequest.model_validate(start_payload(idea_id=56))
    )
    assert response.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("fabricated", "omitted"))
async def test_final_relations_preserve_declared_requirement_coverage(
    failure: str,
) -> None:
    class InvalidRequirementModel(DeterministicPlanningModel):
        async def generate(self, stage, schema, payload):
            value = await super().generate(stage, schema, payload)
            if failure == "omitted" and schema is RequirementsDraft:
                draft = cast(RequirementsDraft, value)
                return draft.model_copy(update={"requirements": ("REQ-1", "REQ-OMITTED")})
            if failure == "fabricated" and schema is RelationDraft:
                draft = cast(RelationDraft, value)
                invalid = draft.items[0].model_copy(
                    update={"source_requirements": ("REQ-FABRICATED",)}
                )
                return RelationDraft(items=(invalid,))
            return value

    service, _, _, _ = make_service(model=InvalidRequirementModel())
    expected = "undeclared" if failure == "fabricated" else "omits"
    with pytest.raises(ValueError, match=expected):
        await service.start(StartPlanRequest.model_validate(start_payload(idea_id=70)))


def test_request_models_forbid_uncontracted_identity_and_invalid_shape() -> None:
    payload = start_payload(idea_id=47)
    payload["thread_id"] = "caller:chosen"
    with pytest.raises(ValidationError):
        StartPlanRequest.model_validate(payload)


@pytest.mark.parametrize(
    "mutation",
    (
        "idea_extra",
        "naive_idea_time",
        "naive_snapshot_time",
        "idea_digest",
        "repository_digest",
    ),
)
def test_snapshot_binding_and_aware_dates_fail_validation(
    mutation: str,
) -> None:
    payload = start_payload(idea_id=59)
    if mutation == "idea_extra":
        payload["idea"]["untrusted"] = "arbitrary"
    elif mutation == "naive_idea_time":
        payload["idea"]["updated_at"] = "2026-07-30T00:00:00"
    elif mutation == "naive_snapshot_time":
        payload["openproject_snapshot"]["captured_at"] = "2026-07-30T00:00:00"
    elif mutation == "idea_digest":
        payload["idea_sha256"] = "c" * 64
    else:
        payload["repositories"][0]["snapshot_sha256"] = "c" * 64
    with pytest.raises(ValidationError):
        StartPlanRequest.model_validate(payload)


def test_repository_snapshots_reject_duplicate_paths() -> None:
    payload = start_payload(idea_id=65)
    file = RepositoryFile.model_validate(payload["repositories"][0]["files"][0])
    payload["repositories"][0]["files"] = [
        file.model_dump(mode="json"),
        file.model_dump(mode="json"),
    ]
    payload["repositories"][0]["snapshot_sha256"] = repository_snapshot_digest(
        "Acme/service",
        "a" * 40,
        (file, file),
    )
    with pytest.raises(ValidationError, match="duplicate file paths"):
        StartPlanRequest.model_validate(payload)


def test_repository_context_has_an_aggregate_utf8_byte_limit() -> None:
    payload = start_payload(idea_id=66)
    content = "x" * 262_144
    digest = hashlib.sha256(content.encode()).hexdigest()
    count = MAX_PLANNER_REQUEST_BYTES // len(content.encode()) + 1
    files = tuple(
        RepositoryFile(
            path=f"context/{index}.txt",
            sha256=digest,
            content=content,
        )
        for index in range(count)
    )
    payload["repositories"][0]["files"] = [file.model_dump(mode="json") for file in files]
    payload["repositories"][0]["snapshot_sha256"] = repository_snapshot_digest(
        "Acme/service",
        "a" * 40,
        files,
    )
    with pytest.raises(ValidationError, match="exceeds"):
        StartPlanRequest.model_validate(payload)


def test_resume_requires_aware_timestamp() -> None:
    with pytest.raises(ValidationError):
        ResumePlanRequest.model_validate(
            {
                "event": {
                    "idempotency_key": "resume:naive:001",
                    "trace_id": str(uuid4()),
                },
                "interrupt_id": "interrupt",
                "comment_id": 1,
                "comment_created_at": "2026-07-31T00:00:00",
                "answer": "Answer",
            }
        )


@pytest.mark.asyncio
async def test_persisted_summary_redacts_tokens_pem_and_high_entropy_values() -> None:
    credential = "AbCdEfGhIjKlMnOpQrStUvWxYz1234567890"
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
    secrets_text = (
        "token=visible-secret "
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 "
        "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 "
        f"{credential}\n{pem}"
    )

    class EchoRepositoryModel(DeterministicPlanningModel):
        async def generate(self, stage, schema, payload):
            if stage == "retrieve_repository_context":
                return DocumentSection(title="Repository summary", body=secrets_text)
            return await super().generate(stage, schema, payload)

    payload = start_payload(idea_id=60)
    file = RepositoryFile(
        path="credentials.txt",
        sha256=hashlib.sha256(secrets_text.encode()).hexdigest(),
        content=secrets_text,
    )
    payload["repositories"][0]["files"] = [file.model_dump(mode="json")]
    payload["repositories"][0]["snapshot_sha256"] = repository_snapshot_digest(
        "Acme/service", "a" * 40, (file,)
    )
    service, _, _, graph = make_service(model=EchoRepositoryModel())
    response, _ = await service.start(StartPlanRequest.model_validate(payload))
    state = await graph.aget_state({"configurable": {"thread_id": response.thread_id}})
    persisted = repr(state.values)
    for forbidden in (
        "visible-secret",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        credential,
        "BEGIN PRIVATE KEY",
    ):
        assert forbidden not in persisted


@pytest.mark.asyncio
async def test_persisted_questions_are_sanitized_before_interrupt() -> None:
    secret = "token=question-visible-secret"

    class SecretQuestionModel(DeterministicPlanningModel):
        async def generate(self, stage, schema, payload):
            if schema is ConsequentialQuestions:
                return ConsequentialQuestions(
                    questions=(f"Should we use {secret}?",),
                    impact=f"The answer changes {secret}.",
                )
            return await super().generate(stage, schema, payload)

    service, _, _, graph = make_service(model=SecretQuestionModel())
    response, _ = await service.start(StartPlanRequest.model_validate(start_payload(idea_id=61)))
    assert response.status == "needs_input"
    snapshot = await graph.aget_state({"configurable": {"thread_id": response.thread_id}})
    assert secret not in repr(snapshot)
    assert secret not in repr(response)


@pytest.mark.asyncio
async def test_critic_findings_and_terminal_failure_are_sanitized() -> None:
    secret = "token=critic-visible-secret"

    class PersistedFindingModel(DeterministicPlanningModel):
        async def generate(self, stage, schema, payload):
            if schema is DecompositionCritique:
                return DecompositionCritique(
                    acceptable=True,
                    findings=(secret,),
                )
            return await super().generate(stage, schema, payload)

    service, _, _, graph = make_service(model=PersistedFindingModel())
    response, _ = await service.start(StartPlanRequest.model_validate(start_payload(idea_id=62)))
    snapshot = await graph.aget_state({"configurable": {"thread_id": response.thread_id}})
    assert secret not in repr(snapshot)

    class FailedFindingModel(DeterministicPlanningModel):
        async def generate(self, stage, schema, payload):
            if schema is DecompositionCritique:
                return DecompositionCritique(
                    acceptable=False,
                    findings=(secret,),
                )
            return await super().generate(stage, schema, payload)

    failed_service, _, _, failed_graph = make_service(model=FailedFindingModel())
    request = StartPlanRequest.model_validate(start_payload(idea_id=63))
    failed, _ = await failed_service.start(request)
    assert failed.status == "failed"
    failed_snapshot = await failed_graph.aget_state(
        {"configurable": {"thread_id": "openproject:63:planning:1"}}
    )
    assert secret not in repr(failed_snapshot)


class CrashOnceRepository(InMemoryIdempotencyRepository):
    def __init__(self) -> None:
        super().__init__()
        self.crash_on_finalize = False

    async def finalize(self, claim, response) -> None:
        if self.crash_on_finalize:
            self.crash_on_finalize = False
            raise RuntimeError("simulated crash after durable graph write")
        await super().finalize(claim, response)


@pytest.mark.asyncio
async def test_start_crash_window_recovers_terminal_graph_without_rerun() -> None:
    repository = CrashOnceRepository()
    model = DeterministicPlanningModel()
    service, saver, _, _ = make_service(idempotency=repository, model=model)
    request = StartPlanRequest.model_validate(start_payload(idea_id=48))
    repository.crash_on_finalize = True
    with pytest.raises(RuntimeError, match="simulated crash"):
        await service.start(request)
    with pytest.raises(IdempotencyInProgress):
        await service.start(request)
    repository.expire_for_test(request.event.idempotency_key)
    restarted, _, _, _ = make_service(checkpointer=saver, idempotency=repository, model=model)
    recovered, replayed = await restarted.start(request)
    assert replayed
    assert recovered.status == "artifacts_ready"


@pytest.mark.asyncio
async def test_partial_checkpoint_continues_after_expired_start_claim() -> None:
    class FailArchitectureOnce(DeterministicPlanningModel):
        failed = False

        async def generate(self, stage, schema, payload):
            if stage == "generate_architecture" and not self.failed:
                self.failed = True
                raise RuntimeError("transient model failure")
            return await super().generate(stage, schema, payload)

    repository = InMemoryIdempotencyRepository()
    model = FailArchitectureOnce()
    service, saver, _, _ = make_service(idempotency=repository, model=model)
    request = StartPlanRequest.model_validate(start_payload(idea_id=49))
    with pytest.raises(RuntimeError, match="transient"):
        await service.start(request)
    repository.expire_for_test(request.event.idempotency_key)
    restarted, _, _, _ = make_service(checkpointer=saver, idempotency=repository, model=model)
    recovered, replayed = await restarted.start(request)
    assert replayed
    assert recovered.status == "artifacts_ready"


@pytest.mark.asyncio
async def test_concurrent_duplicate_gets_typed_in_progress_conflict() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingModel(DeterministicPlanningModel):
        async def generate(self, stage, schema, payload):
            if stage == "classify_scope_and_risk":
                entered.set()
                await release.wait()
            return await super().generate(stage, schema, payload)

    service, _, _, _ = make_service(model=BlockingModel())
    app = create_app(service, internal_token=INTERNAL_TOKEN)
    payload = start_payload(idea_id=50)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=AUTH_HEADERS,
    ) as client:
        first = asyncio.create_task(client.post("/v1/plans", json=payload))
        await entered.wait()
        duplicate = await client.post("/v1/plans", json=payload)
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "thread_execution_in_progress"
        release.set()
        response = await first
        assert response.status_code == 201
        assert response.json()["status"] == "artifacts_ready"


@pytest.mark.asyncio
async def test_heartbeat_prevents_reclaim_during_run_longer_than_lease() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class LongRunningModel(DeterministicPlanningModel):
        async def generate(self, stage, schema, payload):
            if stage == "classify_scope_and_risk":
                entered.set()
                await release.wait()
            return await super().generate(stage, schema, payload)

    class CountingRepository(InMemoryIdempotencyRepository):
        renewals = 0

        async def renew(self, claim):
            self.renewals += 1
            return await super().renew(claim)

    repository = CountingRepository(lease_seconds=1)
    service, _, _, _ = make_service(model=LongRunningModel(), idempotency=repository)
    request = StartPlanRequest.model_validate(start_payload(idea_id=54))
    first = asyncio.create_task(service.start(request))
    await entered.wait()
    await asyncio.sleep(1.2)
    assert repository.renewals >= 2
    with pytest.raises(ExecutionInProgress):
        await service.start(request)
    release.set()
    response, _ = await first
    assert response.status == "artifacts_ready"


@pytest.mark.asyncio
async def test_resume_crash_recovers_without_consuming_answer_twice() -> None:
    repository = CrashOnceRepository()
    service, saver, _, _ = make_service(idempotency=repository)
    request = StartPlanRequest.model_validate(
        start_payload(idea_id=51, description="Choose? crash recovery")
    )
    pending, _ = await service.start(request)
    assert pending.interrupt is not None
    resume = ResumePlanRequest.model_validate(
        {
            "event": {
                "idempotency_key": "resume:crash:0001",
                "trace_id": str(uuid4()),
            },
            "interrupt_id": pending.interrupt.interrupt_id,
            "comment_id": 9051,
            "comment_created_at": after_timestamp(pending.interrupt.created_at),
            "answer": "Use option A.",
        }
    )
    repository.crash_on_finalize = True
    with pytest.raises(RuntimeError, match="simulated crash"):
        await service.resume(pending.thread_id, resume)
    repository.expire_for_test(resume.event.idempotency_key)
    restarted, _, _, graph = make_service(checkpointer=saver, idempotency=repository)
    recovered, replayed = await restarted.resume(pending.thread_id, resume)
    assert replayed
    assert recovered.status == "artifacts_ready"
    state = await graph.aget_state({"configurable": {"thread_id": pending.thread_id}})
    assert state.values["human_answer"] == "Use option A."


@pytest.mark.asyncio
async def test_distinct_resume_key_cannot_take_over_an_unfinished_thread_mutation() -> None:
    class PauseFirstResumeClaim(InMemoryIdempotencyRepository):
        def __init__(self) -> None:
            super().__init__()
            self.claimed = asyncio.Event()
            self.paused = False

        async def claim(self, **kwargs):
            result = await super().claim(**kwargs)
            if (
                kwargs["kind"] == "resume"
                and isinstance(result, IdempotencyClaim)
                and not self.paused
            ):
                self.paused = True
                self.claimed.set()
                await asyncio.Event().wait()
            return result

    repository = PauseFirstResumeClaim()
    service, _, _, graph = make_service(idempotency=repository)
    pending, _ = await service.start(
        StartPlanRequest.model_validate(
            start_payload(idea_id=67, description="Choose? concurrent resume")
        )
    )
    assert pending.interrupt is not None
    first = ResumePlanRequest.model_validate(
        {
            "event": {
                "idempotency_key": "resume:concurrent:first",
                "trace_id": str(uuid4()),
            },
            "interrupt_id": pending.interrupt.interrupt_id,
            "comment_id": 9067,
            "comment_created_at": after_timestamp(pending.interrupt.created_at),
            "answer": "Use the first answer.",
        }
    )
    second = ResumePlanRequest.model_validate(
        {
            "event": {
                "idempotency_key": "resume:concurrent:second",
                "trace_id": str(uuid4()),
            },
            "interrupt_id": pending.interrupt.interrupt_id,
            "comment_id": 9068,
            "comment_created_at": after_timestamp(
                pending.interrupt.created_at,
                seconds=2,
            ),
            "answer": "Use the second answer.",
        }
    )

    first_run = asyncio.create_task(service.resume(pending.thread_id, first))
    await asyncio.wait_for(repository.claimed.wait(), timeout=1)
    with pytest.raises(ExecutionInProgress):
        await service.resume(pending.thread_id, second)
    first_run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_run

    repository.expire_for_test(first.event.idempotency_key)
    with pytest.raises(IdempotencyConflict) as blocked:
        await service.resume(pending.thread_id, second)
    assert type(blocked.value) is IdempotencyConflict

    recovered, replayed = await service.resume(pending.thread_id, first)
    assert replayed
    assert recovered.status == "artifacts_ready"
    state = await graph.aget_state({"configurable": {"thread_id": pending.thread_id}})
    assert state.values["human_answer"] == "Use the first answer."


@pytest.mark.asyncio
async def test_cancelling_service_call_cancels_and_awaits_graph_operation() -> None:
    entered = asyncio.Event()
    operation_finished = asyncio.Event()
    release = asyncio.Event()

    class CancellationModel(DeterministicPlanningModel):
        calls = 0

        async def generate(self, stage, schema, payload):
            if stage == "classify_scope_and_risk":
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    try:
                        await release.wait()
                    finally:
                        operation_finished.set()
            return await super().generate(stage, schema, payload)

    repository = InMemoryIdempotencyRepository()
    model = CancellationModel()
    service, _, _, _ = make_service(idempotency=repository, model=model)
    request = StartPlanRequest.model_validate(start_payload(idea_id=68))
    running = asyncio.create_task(service.start(request))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.wait_for(operation_finished.wait(), timeout=1)

        repository.expire_for_test(request.event.idempotency_key)
        recovered, replayed = await asyncio.wait_for(
            service.start(request),
            timeout=2,
        )
        assert replayed
        assert recovered.status == "artifacts_ready"
        assert model.calls == 2
    finally:
        release.set()


@pytest.mark.asyncio
async def test_hard_guard_fences_stale_owner_that_suppresses_cancellation() -> None:
    entered = asyncio.Event()
    suppressed = asyncio.Event()
    release = asyncio.Event()

    class StaleOwnerModel(DeterministicPlanningModel):
        calls = 0

        async def generate(self, stage, schema, payload):
            if stage == "classify_scope_and_risk":
                self.calls += 1
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    suppressed.set()
                    await release.wait()
            return await super().generate(stage, schema, payload)

    saver = InMemorySaver()
    repository = InMemoryIdempotencyRepository()
    model = StaleOwnerModel()
    graph = build_planner_graph(model, saver)
    guard = InMemoryExecutionGuard(graph)
    first_service = PlannerService(graph, repository, guard)
    restarted_service = PlannerService(graph, repository, guard)
    request = StartPlanRequest.model_validate(start_payload(idea_id=71))
    running = asyncio.create_task(first_service.start(request))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        repository.expire_for_test(request.event.idempotency_key)
        running.cancel()
        await asyncio.wait_for(suppressed.wait(), timeout=1)
        assert not running.done()

        before = repr(
            await graph.aget_state({"configurable": {"thread_id": "openproject:71:planning:1"}})
        )
        with pytest.raises(ExecutionInProgress):
            await restarted_service.start(request)
        after = repr(
            await graph.aget_state({"configurable": {"thread_id": "openproject:71:planning:1"}})
        )
        assert after == before
        assert model.calls == 1

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=2)
        recovered, replayed = await restarted_service.start(request)
        assert replayed
        assert recovered.status == "artifacts_ready"
        assert model.calls == 1
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_dedicated_connection_close_is_awaited_under_cancellation() -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    close_finished = asyncio.Event()

    class SlowConnection:
        async def close(self) -> None:
            close_started.set()
            await allow_close.wait()
            close_finished.set()

    closing = asyncio.create_task(_close_connection(cast(Any, SlowConnection())))
    await asyncio.wait_for(close_started.wait(), timeout=1)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, timeout=1)
    assert close_finished.is_set()


@pytest.mark.asyncio
async def test_idempotency_key_cannot_replay_across_threads_or_kinds() -> None:
    service, _, _, _ = make_service()
    first = StartPlanRequest.model_validate(
        start_payload(idea_id=52, idempotency_key="shared:key:0001")
    )
    await service.start(first)
    other = StartPlanRequest.model_validate(
        start_payload(idea_id=53, idempotency_key="shared:key:0001")
    )
    with pytest.raises(IdempotencyConflict, match="kind, thread"):
        await service.start(other)
