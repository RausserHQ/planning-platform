"""Typed, internal-only client for the private planner API."""

from __future__ import annotations

import math
from typing import Any

import httpx

from planning_platform.planner.models import (
    ArtifactBundle,
    PlanResponse,
    ResumePlanRequest,
    StartPlanRequest,
)


class PlannerClientError(RuntimeError):
    pass


class PlannerThreadNotFound(PlannerClientError):
    pass


class PlannerClient:
    """A narrow HTTP seam that carries a trace ID only through typed request bodies."""

    def __init__(
        self,
        base_url: str,
        internal_token: str,
        client: httpx.AsyncClient,
        *,
        timeout_seconds: float = 600.0,
    ) -> None:
        if not base_url.startswith("https://") and not base_url.startswith("http://"):
            raise ValueError("planner URL must be absolute")
        if not internal_token:
            raise ValueError("planner internal token is required")
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 30
            or timeout_seconds > 900
        ):
            raise ValueError("planner timeout must be between 31 and 900 seconds")
        self._base_url = base_url.rstrip("/")
        self._token = internal_token
        self._client = client
        self._timeout = httpx.Timeout(float(timeout_seconds))

    async def start(self, request: StartPlanRequest) -> PlanResponse:
        return PlanResponse.model_validate(await self._request("POST", "/v1/plans", request))

    async def get(self, thread_id: str) -> PlanResponse:
        return PlanResponse.model_validate(await self._request("GET", f"/v1/plans/{thread_id}"))

    async def resume(self, thread_id: str, request: ResumePlanRequest) -> PlanResponse:
        return PlanResponse.model_validate(
            await self._request("POST", f"/v1/plans/{thread_id}/resume", request)
        )

    async def artifacts(self, thread_id: str) -> ArtifactBundle:
        response = await self._request("GET", f"/v1/plans/{thread_id}/artifacts")
        return ArtifactBundle.model_validate(response)

    async def _request(
        self, method: str, path: str, body: StartPlanRequest | ResumePlanRequest | None = None
    ) -> dict[str, Any]:
        response = await self._client.request(
            method,
            f"{self._base_url}{path}",
            headers={"X-Planning-Internal-Token": self._token},
            json=None if body is None else body.model_dump(mode="json"),
            timeout=self._timeout,
        )
        if response.status_code == 404:
            raise PlannerThreadNotFound("planner thread was not found")
        if response.status_code >= 400:
            raise PlannerClientError(f"planner request failed with status {response.status_code}")
        try:
            value = response.json()
        except ValueError as error:
            raise PlannerClientError("planner response is not JSON") from error
        if not isinstance(value, dict):
            raise PlannerClientError("planner response must be an object")
        return value
