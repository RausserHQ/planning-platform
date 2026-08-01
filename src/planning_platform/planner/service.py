"""Planner application service: durable claims, recovery, interrupt, and resume."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Coroutine
from datetime import datetime
from typing import Any, TypeVar, cast

from langgraph.types import Command

from .execution import ExecutionGuard
from .graph import PlannerRuntimeContext, sanitize_checkpoint_text
from .idempotency import (
    IdempotencyClaim,
    IdempotencyRepository,
    IdempotentReplay,
)
from .models import (
    ArtifactBundle,
    ArtifactContent,
    ArtifactManifestEntry,
    PendingInterrupt,
    PlanResponse,
    ResumeBinding,
    ResumePlanRequest,
    StartPlanRequest,
    derive_thread_id,
)

Result = TypeVar("Result")


class PlanNotFound(LookupError):
    pass


class ResumeConflict(ValueError):
    pass


class ArtifactsNotReady(ValueError):
    pass


def _request_hash(request: StartPlanRequest | ResumePlanRequest) -> str:
    canonical = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class PlannerService:
    def __init__(
        self,
        read_graph: Any,
        idempotency: IdempotencyRepository,
        execution_guard: ExecutionGuard,
    ) -> None:
        self._read_graph = read_graph
        self._idempotency = idempotency
        self._execution_guard = execution_guard

    async def start(self, request: StartPlanRequest) -> tuple[PlanResponse, bool]:
        thread_id = derive_thread_id(request.idea.work_package_id, request.plan_version)
        body_hash = _request_hash(request)
        async with self._execution_guard.execution(thread_id) as graph:
            claim = await self._idempotency.claim(
                kind="start",
                key=request.event.idempotency_key,
                body_hash=body_hash,
                thread_id=thread_id,
            )
            if isinstance(claim, IdempotentReplay):
                return PlanResponse.model_validate(claim.response), True
            response = await self._with_heartbeat(
                claim,
                self._run_or_recover_start(graph, request, claim),
            )
            serialized = response.model_dump(mode="json")
            await self._idempotency.finalize(claim, serialized)
            return response, claim.recovered

    async def _run_or_recover_start(
        self,
        graph: Any,
        request: StartPlanRequest,
        claim: IdempotencyClaim,
    ) -> PlanResponse:
        thread_id = claim.thread_id
        config = {"configurable": {"thread_id": thread_id}}
        state = await self._state_optional(graph, thread_id)
        if state is not None and _is_servable(state):
            return self._response_from_state(thread_id, state)
        runtime = PlannerRuntimeContext(request.repositories)
        if state is not None:
            await graph.ainvoke(None, config, context=runtime)
            return await self._response(graph, thread_id)
        snapshot = request.openproject_snapshot
        idea = request.idea.model_dump(mode="json")
        idea["title"] = sanitize_checkpoint_text(str(idea["title"]))
        idea["description"] = sanitize_checkpoint_text(str(idea.get("description", "")))
        initial = {
            "thread_id": thread_id,
            "trace_id": str(request.event.trace_id),
            "status": "planning",
            "plan_id": request.plan_id,
            "plan_version": request.plan_version,
            "idea": idea,
            "openproject_snapshot": snapshot.model_dump(mode="json"),
            "repositories": [
                {"name": repository.name, "commit": repository.commit}
                for repository in request.repositories
            ],
        }
        if request.replan is not None:
            initial["replan"] = {
                "base_approved_planning_commit": (request.replan.base_approved_planning_commit),
                "selected_root_keys": list(request.replan.selected_root_keys),
                "affected_node_keys": list(request.replan.affected_node_keys),
                "reason": sanitize_checkpoint_text(request.replan.reason, limit=4_096),
                "prior_plan": request.replan.prior_plan.model_dump(mode="json"),
            }
        await graph.ainvoke(initial, config, context=runtime)
        return await self._response(graph, thread_id)

    async def get(self, thread_id: str) -> PlanResponse:
        return await self._response(self._read_graph, thread_id)

    async def artifacts(self, thread_id: str) -> ArtifactBundle:
        state = await self._state(self._read_graph, thread_id)
        if state.get("status") != "artifacts_ready":
            raise ArtifactsNotReady("planning artifacts are not ready")
        artifacts = cast(dict[str, str], state.get("artifacts", {}))
        content = []
        for path, value in sorted(artifacts.items()):
            digest = hashlib.sha256(value.encode()).hexdigest()
            content.append(ArtifactContent(path=path, sha256=digest, content=value))
        manifest = {
            entry.path: entry.sha256
            for entry in self._response_from_state(thread_id, state).artifact_manifest
        }
        if {artifact.path: artifact.sha256 for artifact in content} != manifest:
            raise ValueError("stored artifact bytes do not match artifact manifest")
        return ArtifactBundle(thread_id=thread_id, artifacts=tuple(content))

    async def resume(self, thread_id: str, request: ResumePlanRequest) -> tuple[PlanResponse, bool]:
        binding = ResumeBinding(
            interrupt_id=request.interrupt_id,
            comment_id=request.comment_id,
            comment_created_at=request.comment_created_at,
        )
        async with self._execution_guard.execution(thread_id) as graph:
            self._validate_resume_state(await self._state(graph, thread_id), request, binding)
            claim = await self._idempotency.claim(
                kind="resume",
                key=request.event.idempotency_key,
                body_hash=_request_hash(request),
                thread_id=thread_id,
                resume_binding=binding,
            )
            if isinstance(claim, IdempotentReplay):
                return PlanResponse.model_validate(claim.response), True
            response = await self._with_heartbeat(
                claim,
                self._run_or_recover_resume(graph, thread_id, request, binding),
            )
            await self._idempotency.finalize(claim, response.model_dump(mode="json"))
            return response, claim.recovered

    def _validate_resume_state(
        self,
        state: dict[str, Any],
        request: ResumePlanRequest,
        binding: ResumeBinding,
    ) -> None:
        consumed = state.get("consumed_resume")
        expected = binding.model_dump(mode="json")
        if isinstance(consumed, dict):
            if consumed != expected:
                raise ResumeConflict("thread consumed a different resume binding")
            return
        pending = state.get("pending_interrupt")
        if not isinstance(pending, dict):
            raise ResumeConflict("planning thread has no pending interrupt")
        if pending.get("interrupt_id") != request.interrupt_id:
            raise ResumeConflict("resume does not match the pending interrupt")
        if request.comment_created_at <= _timestamp(str(pending["created_at"])):
            raise ResumeConflict("resume comment is not newer than the pending interrupt")

    async def _run_or_recover_resume(
        self,
        graph: Any,
        thread_id: str,
        request: ResumePlanRequest,
        binding: ResumeBinding,
    ) -> PlanResponse:
        state = await self._state(graph, thread_id)
        consumed = state.get("consumed_resume")
        expected_binding = binding.model_dump(mode="json")
        if isinstance(consumed, dict):
            if consumed != expected_binding:
                raise ResumeConflict("thread consumed a different resume binding")
            if _is_servable(state):
                return self._response_from_state(thread_id, state)
            await graph.ainvoke(
                None,
                {"configurable": {"thread_id": thread_id}},
                context=PlannerRuntimeContext(()),
            )
            return await self._response(graph, thread_id)
        pending = state.get("pending_interrupt")
        if not isinstance(pending, dict):
            raise ResumeConflict("planning thread has no pending interrupt")
        if pending.get("interrupt_id") != request.interrupt_id:
            raise ResumeConflict("resume does not match the pending interrupt")
        if request.comment_created_at <= _timestamp(str(pending["created_at"])):
            raise ResumeConflict("resume comment is not newer than the pending interrupt")
        await graph.ainvoke(
            Command(
                resume={
                    "answer": sanitize_checkpoint_text(request.answer),
                    "trace_id": str(request.event.trace_id),
                    **expected_binding,
                }
            ),
            {"configurable": {"thread_id": thread_id}},
            context=PlannerRuntimeContext(()),
        )
        return await self._response(graph, thread_id)

    async def ready(self) -> bool:
        return await self._idempotency.ready()

    async def _with_heartbeat(
        self, claim: IdempotencyClaim, operation: Coroutine[Any, Any, Result]
    ) -> Result:
        stop = asyncio.Event()

        async def heartbeat() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(
                        stop.wait(),
                        timeout=self._idempotency.heartbeat_interval_seconds,
                    )
                except TimeoutError:
                    await self._idempotency.renew(claim)

        heartbeat_task = asyncio.create_task(heartbeat())
        operation_task = asyncio.create_task(operation)
        try:
            done, _ = await asyncio.wait(
                (heartbeat_task, operation_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                error = heartbeat_task.exception()
                if error is not None:
                    operation_task.cancel()
                    await asyncio.gather(operation_task, return_exceptions=True)
                    raise error
                raise RuntimeError("claim heartbeat stopped before graph execution")
            return await operation_task
        finally:
            if not operation_task.done():
                operation_task.cancel()
            stop.set()
            await asyncio.gather(operation_task, return_exceptions=True)
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _state_optional(self, graph: Any, thread_id: str) -> dict[str, Any] | None:
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        values = dict(snapshot.values)
        return values or None

    async def _state(self, graph: Any, thread_id: str) -> dict[str, Any]:
        values = await self._state_optional(graph, thread_id)
        if values is None:
            raise PlanNotFound(thread_id)
        return values

    async def _response(self, graph: Any, thread_id: str) -> PlanResponse:
        return self._response_from_state(thread_id, await self._state(graph, thread_id))

    def _response_from_state(self, thread_id: str, state: dict[str, Any]) -> PlanResponse:
        pending = state.get("pending_interrupt")
        status = str(state.get("status", "planning"))
        if isinstance(pending, dict):
            status = "needs_input"
        manifest = tuple(
            ArtifactManifestEntry.model_validate(entry)
            for entry in state.get("artifact_manifest", [])
        )
        return PlanResponse(
            thread_id=thread_id,
            status=status,  # type: ignore[arg-type]
            trace_id=str(state["trace_id"]),
            interrupt=(
                PendingInterrupt.model_validate(pending) if isinstance(pending, dict) else None
            ),
            artifact_manifest=manifest,
        )


def _is_servable(state: dict[str, Any]) -> bool:
    return isinstance(state.get("pending_interrupt"), dict) or state.get("status") in {
        "artifacts_ready",
        "failed",
    }
