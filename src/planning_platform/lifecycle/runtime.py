"""Environment-wired production runtime used only by Windmill workers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx

from planning_platform.github_adapter import (
    GitHubAdapter,
    GitHubAppInstallationToken,
)
from planning_platform.openproject_adapter import OpenProjectPublicationAdapter
from planning_platform.openproject_transport import discover_openproject_adapter
from planning_platform.publication_journal import PostgresPublicationJournal

from .dedupe import PostgresDeliveryDeduplicator
from .planner_client import PlannerClient
from .recovery import RecoveryCipher
from .service import LifecycleService
from .store import PostgresLifecycleStore


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value


def _implementation_required_checks() -> dict[str, tuple[str, ...]]:
    try:
        value: Any = json.loads(_required("PLANNING_IMPLEMENTATION_REQUIRED_CHECKS_JSON"))
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "PLANNING_IMPLEMENTATION_REQUIRED_CHECKS_JSON must be JSON"
        ) from error
    if not isinstance(value, dict):
        raise RuntimeError(
            "PLANNING_IMPLEMENTATION_REQUIRED_CHECKS_JSON must be an object"
        )
    result: dict[str, tuple[str, ...]] = {}
    for repository, checks in value.items():
        if (
            not isinstance(repository, str)
            or not isinstance(checks, list)
            or not checks
            or not all(isinstance(check, str) for check in checks)
        ):
            raise RuntimeError(
                "PLANNING_IMPLEMENTATION_REQUIRED_CHECKS_JSON has an invalid entry"
            )
        result[repository] = tuple(checks)
    return result


def _positive_integer(name: str) -> int:
    try:
        value = int(_required(name))
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _bounded_integer(
    name: str,
    *,
    minimum: int,
    maximum: int,
    default: int | None = None,
) -> int:
    raw = os.environ.get(name)
    raw = str(default) if raw is None and default is not None else _required(name)
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}") from error
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass
class LifecycleRuntime:
    service: LifecycleService
    deduplicator: PostgresDeliveryDeduplicator
    store: PostgresLifecycleStore
    _async_client: httpx.AsyncClient
    _openproject: OpenProjectPublicationAdapter

    @classmethod
    def from_environment(cls) -> LifecycleRuntime:
        lifecycle_database = _required("PLANNING_LIFECYCLE_DATABASE_URL")
        planner_timeout_seconds = _bounded_integer(
            "PLANNER_HTTP_TIMEOUT_SECONDS",
            minimum=31,
            maximum=900,
            default=600,
        )
        openproject_url = _required("OPENPROJECT_BASE_URL")
        openproject_token = _required("OPENPROJECT_API_TOKEN")
        openproject = discover_openproject_adapter(
            base_url=openproject_url,
            canonical_origin=_required("OPENPROJECT_CANONICAL_ORIGIN"),
            project_identifier=_required("OPENPROJECT_PROJECT_IDENTIFIER"),
            token=openproject_token,
        )
        async_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        token_provider = GitHubAppInstallationToken(
            async_client,
            app_id=int(_required("GITHUB_APP_ID")),
            installation_id=int(_required("GITHUB_APP_INSTALLATION_ID")),
            private_key_pem=_required("GITHUB_APP_PRIVATE_KEY"),
        )
        github = GitHubAdapter(async_client, token_provider)
        planner = PlannerClient(
            _required("PLANNER_URL"),
            _required("PLANNER_INTERNAL_TOKEN"),
            async_client,
            timeout_seconds=planner_timeout_seconds,
        )
        store = PostgresLifecycleStore(lifecycle_database)
        dedupe = PostgresDeliveryDeduplicator(lifecycle_database)
        publication = PostgresPublicationJournal(lifecycle_database)
        publication_ready = publication.ready()
        publication.close()
        if not store.ready() or not dedupe.ready() or not publication_ready:
            openproject.close()
            raise RuntimeError("planning lifecycle schema is not ready; run explicit migrations")
        try:
            implementation_stale_hours = _positive_integer(
                "PLANNING_IMPLEMENTATION_STALE_HOURS"
            )
            planning_thread_stale_seconds = _positive_integer(
                "PLANNING_THREAD_STALE_SECONDS"
            )
        except RuntimeError:
            openproject.close()
            raise
        return cls(
            service=LifecycleService(
                planner=planner,
                openproject=openproject,
                github=github,
                store=store,
                publication_database_url=lifecycle_database,
                recovery_cipher=RecoveryCipher.from_base64(_required("PLANNING_RECOVERY_KEY_B64")),
                planning_repository=_required("PLANNING_ARTIFACT_REPOSITORY"),
                implementation_required_checks=_implementation_required_checks(),
                implementation_stale_after=timedelta(
                    hours=implementation_stale_hours
                ),
                planning_thread_stale_after=timedelta(
                    seconds=planning_thread_stale_seconds
                ),
            ),
            deduplicator=dedupe,
            store=store,
            _async_client=async_client,
            _openproject=openproject,
        )

    async def close(self) -> None:
        self.deduplicator.close()
        self._openproject.close()
        await self._async_client.aclose()

    async def __aenter__(self) -> LifecycleRuntime:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
