"""Private FastAPI surface for the planner graph."""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Path, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .execution import ExecutionInProgress, PostgresExecutionGuard
from .graph import build_planner_graph
from .idempotency import (
    IdempotencyConflict,
    IdempotencyInProgress,
    PostgresIdempotencyRepository,
)
from .model import (
    ChatOpenAIPlanningModel,
    validate_model_configuration,
)
from .models import (
    AbandonTerminalResumeRequest,
    ArtifactBundle,
    PlanResponse,
    ResumePlanRequest,
    StartPlanRequest,
)
from .service import (
    ArtifactsNotReady,
    PlannerService,
    PlanNotFound,
    ResumeConflict,
)

PLANS_STARTED = Counter("planning_platform_plans_started_total", "New planner threads started")
PLANS_RESUMED = Counter("planning_platform_plans_resumed_total", "Planner interrupts resumed")
THREAD_PATTERN = r"^openproject:[1-9][0-9]*:planning:[1-9][0-9]*$"
INTERNAL_TOKEN = APIKeyHeader(
    name="X-Planning-Internal-Token",
    auto_error=False,
    scheme_name="InternalToken",
    description="Internal credential for authenticated planner operations.",
)


@dataclass(frozen=True)
class PlannerSettings:
    database_url: str
    model: str
    reasoning_effort: str
    internal_token: str

    @classmethod
    def from_environment(cls) -> PlannerSettings:
        database_url = os.environ.get("PLANNER_DATABASE_URL")
        if not database_url:
            raise RuntimeError("PLANNER_DATABASE_URL is required")
        model = os.environ.get("PLANNER_OPENAI_MODEL")
        if not model:
            raise RuntimeError("PLANNER_OPENAI_MODEL is required")
        reasoning_effort = os.environ.get("PLANNER_OPENAI_REASONING_EFFORT", "medium")
        try:
            validate_model_configuration(model, reasoning_effort)
        except ValueError as error:
            raise RuntimeError(str(error)) from error
        internal_token = os.environ.get("PLANNER_INTERNAL_TOKEN")
        if not internal_token:
            raise RuntimeError("PLANNER_INTERNAL_TOKEN is required")
        return cls(
            database_url=database_url,
            model=model,
            reasoning_effort=reasoning_effort,
            internal_token=internal_token,
        )


def _production_lifespan() -> Any:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = PlannerSettings.from_environment()
        pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=10,
            open=False,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
        )
        await pool.open()
        checkpointer = AsyncPostgresSaver(pool)
        idempotency = PostgresIdempotencyRepository(pool)
        model = ChatOpenAIPlanningModel(
            settings.model,
            reasoning_effort=settings.reasoning_effort,
        )
        app.state.service = PlannerService(
            build_planner_graph(model, checkpointer),
            idempotency,
            PostgresExecutionGuard(settings.database_url, model),
        )
        app.state.internal_token = settings.internal_token
        app.state.pool = pool
        try:
            yield
        finally:
            await pool.close()

    return lifespan


def create_app(
    service: PlannerService | None = None, *, internal_token: str | None = None
) -> FastAPI:
    lifespan = None if service is not None else _production_lifespan()
    app = FastAPI(
        title="Planning Platform Planner API",
        version="1.0.0",
        lifespan=lifespan,
    )
    if service is not None:
        app.state.service = service
        app.state.internal_token = internal_token

    def planner(request: Request) -> PlannerService:
        return request.app.state.service  # type: ignore[no-any-return]

    def authenticate_internal(
        request: Request,
        supplied_token: Annotated[str | None, Depends(INTERNAL_TOKEN)] = None,
    ) -> None:
        expected = request.app.state.internal_token
        if (
            not isinstance(expected, str)
            or not isinstance(supplied_token, str)
            or not secrets.compare_digest(expected, supplied_token)
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    def in_progress(error: IdempotencyInProgress) -> JSONResponse:
        return JSONResponse(
            {
                "detail": {
                    "code": "idempotency_in_progress",
                    "message": str(error),
                    "lease_expires_at": error.lease_expires_at.isoformat(),
                }
            },
            status_code=409,
        )

    def execution_in_progress(error: ExecutionInProgress) -> JSONResponse:
        return JSONResponse(
            {
                "detail": {
                    "code": "thread_execution_in_progress",
                    "message": str(error),
                }
            },
            status_code=409,
        )

    @app.get("/health/live", operation_id="live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready", operation_id="ready")
    async def ready(request: Request) -> Response:
        if await planner(request).ready():
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not_ready"}, status_code=503)

    @app.get("/metrics", operation_id="metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post(
        "/v1/plans",
        operation_id="startPlan",
        response_model=PlanResponse,
        dependencies=[Depends(authenticate_internal)],
        responses={
            200: {"description": "Existing idempotent result"},
            401: {"description": "Internal authentication failed"},
            409: {"description": "Idempotency key conflicts with an existing request"},
        },
        status_code=201,
    )
    async def start_plan(body: StartPlanRequest, request: Request) -> PlanResponse | JSONResponse:
        try:
            result, replayed = await planner(request).start(body)
        except ExecutionInProgress as error:
            return execution_in_progress(error)
        except IdempotencyInProgress as error:
            return in_progress(error)
        except IdempotencyConflict as error:
            return JSONResponse({"detail": str(error)}, status_code=409)
        if replayed:
            return JSONResponse(result.model_dump(mode="json"), status_code=200)
        PLANS_STARTED.inc()
        return result

    @app.get(
        "/v1/plans/{thread_id}",
        operation_id="getPlan",
        response_model=PlanResponse,
        dependencies=[Depends(authenticate_internal)],
        responses={
            401: {"description": "Internal authentication failed"},
            404: {"description": "Thread not found"},
        },
    )
    async def get_plan(
        request: Request,
        thread_id: Annotated[str, Path(pattern=THREAD_PATTERN)],
    ) -> PlanResponse | JSONResponse:
        try:
            return await planner(request).get(thread_id)
        except PlanNotFound:
            return JSONResponse({"detail": "planning thread not found"}, status_code=404)

    @app.get(
        "/v1/plans/{thread_id}/artifacts",
        operation_id="getPlanArtifacts",
        response_model=ArtifactBundle,
        dependencies=[Depends(authenticate_internal)],
        responses={
            401: {"description": "Internal authentication failed"},
            404: {"description": "Thread not found"},
            409: {"description": "Artifacts are not ready"},
        },
    )
    async def get_plan_artifacts(
        request: Request,
        thread_id: Annotated[str, Path(pattern=THREAD_PATTERN)],
    ) -> ArtifactBundle | JSONResponse:
        try:
            return await planner(request).artifacts(thread_id)
        except PlanNotFound:
            return JSONResponse({"detail": "planning thread not found"}, status_code=404)
        except ArtifactsNotReady as error:
            return JSONResponse({"detail": str(error)}, status_code=409)

    @app.post(
        "/v1/plans/{thread_id}/resume",
        operation_id="resumePlan",
        response_model=PlanResponse,
        dependencies=[Depends(authenticate_internal)],
        responses={
            401: {"description": "Internal authentication failed"},
            404: {"description": "Thread not found"},
            409: {"description": "Resume does not match durable pending state"},
        },
    )
    async def resume_plan(
        body: ResumePlanRequest,
        request: Request,
        thread_id: Annotated[str, Path(pattern=THREAD_PATTERN)],
    ) -> PlanResponse | JSONResponse:
        try:
            result, replayed = await planner(request).resume(thread_id, body)
        except PlanNotFound:
            return JSONResponse({"detail": "planning thread not found"}, status_code=404)
        except ExecutionInProgress as error:
            return execution_in_progress(error)
        except IdempotencyInProgress as error:
            return in_progress(error)
        except (IdempotencyConflict, ResumeConflict) as error:
            return JSONResponse({"detail": str(error)}, status_code=409)
        if not replayed:
            PLANS_RESUMED.inc()
        return result

    @app.post(
        "/v1/plans/{thread_id}/abandon-terminal-resume",
        operation_id="abandonTerminalResume",
        response_model=PlanResponse,
        dependencies=[Depends(authenticate_internal)],
        responses={
            401: {"description": "Internal authentication failed"},
            404: {"description": "Thread not found"},
            409: {"description": "Terminal resume cannot be safely abandoned"},
        },
    )
    async def abandon_terminal_resume(
        body: AbandonTerminalResumeRequest,
        request: Request,
        thread_id: Annotated[str, Path(pattern=THREAD_PATTERN)],
    ) -> PlanResponse | JSONResponse:
        try:
            return await planner(request).abandon_terminal_resume(thread_id, body)
        except PlanNotFound:
            return JSONResponse({"detail": "planning thread not found"}, status_code=404)
        except ExecutionInProgress as error:
            return execution_in_progress(error)
        except IdempotencyInProgress as error:
            return in_progress(error)
        except (IdempotencyConflict, ResumeConflict) as error:
            return JSONResponse({"detail": str(error)}, status_code=409)

    return app


app = create_app()
