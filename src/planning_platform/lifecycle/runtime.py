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
from planning_platform.openproject_discovery import discover_openproject_config
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
        openproject_url = _required("OPENPROJECT_BASE_URL")
        openproject_token = _required("OPENPROJECT_API_TOKEN")
        config = discover_openproject_config(
            base_url=openproject_url,
            project_identifier=_required("OPENPROJECT_PROJECT_IDENTIFIER"),
            token=openproject_token,
        )
        openproject = OpenProjectPublicationAdapter(config, openproject_token)
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
            implementation_stale_hours = int(
                _required("PLANNING_IMPLEMENTATION_STALE_HOURS")
            )
        except ValueError as error:
            openproject.close()
            raise RuntimeError(
                "PLANNING_IMPLEMENTATION_STALE_HOURS must be an integer"
            ) from error
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
