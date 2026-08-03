from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from planning_platform.planner.execution import (
    ExecutionInProgress,
    PostgresExecutionGuard,
)
from planning_platform.planner.graph import build_planner_graph
from planning_platform.planner.idempotency import (
    IdempotencyClaim,
    IdempotencyConflict,
    IdempotencyInProgress,
    IdempotentReplay,
    PostgresIdempotencyRepository,
)
from planning_platform.planner.migrate import migrate
from planning_platform.planner.model import DeterministicPlanningModel
from planning_platform.planner.models import (
    IdeaSnapshot,
    OpenProjectSnapshotInput,
    RepositoryFile,
    ResumeBinding,
    StartPlanRequest,
    idea_snapshot_digest,
    repository_snapshot_digest,
)
from planning_platform.planner.service import PlannerService

DATABASE_URL = os.environ.get("PLANNER_TEST_DATABASE_URL")


def postgres_pool(
    database_url: str,
    *,
    search_path: str | None = None,
) -> AsyncConnectionPool[Any]:
    kwargs: dict[str, Any] = {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
    }
    if search_path is not None:
        kwargs["options"] = f"-c search_path={search_path}"
    return AsyncConnectionPool(
        database_url,
        min_size=1,
        max_size=3,
        open=False,
        kwargs=kwargs,
    )


def postgres_request(
    unique: str,
    *,
    description: str = "Exercise durable planner state.",
) -> StartPlanRequest:
    content = "immutable context"
    idea = IdeaSnapshot.model_validate(
        {
            "work_package_id": int(unique[:8], 16) + 1,
            "lock_version": 0,
            "updated_at": "2026-07-30T00:00:00Z",
            "title": "Postgres execution guard",
            "description": description,
        }
    )
    snapshot = OpenProjectSnapshotInput.model_validate(
        {
            "captured_at": "2026-07-30T00:00:00Z",
            "etag": unique,
            "sha256": "b" * 64,
        }
    )
    repository_file = RepositoryFile(
        path="README.md",
        sha256=hashlib.sha256(content.encode()).hexdigest(),
        content=content,
    )
    return StartPlanRequest.model_validate(
        {
            "event": {
                "idempotency_key": f"postgres:start:{unique}",
                "trace_id": str(uuid4()),
            },
            "idea": idea.model_dump(mode="json"),
            "plan_id": f"postgres-{unique[:12]}",
            "plan_version": 1,
            "idea_sha256": idea_snapshot_digest(idea, snapshot),
            "openproject_snapshot": snapshot.model_dump(mode="json"),
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
    )


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
async def test_postgres_checkpoint_and_idempotent_restart() -> None:
    assert DATABASE_URL is not None
    await migrate(DATABASE_URL)
    pool = postgres_pool(DATABASE_URL)
    await pool.open()
    try:
        repository = PostgresIdempotencyRepository(pool)
        checkpointer = AsyncPostgresSaver(pool)
        content = "immutable context"
        unique = uuid4().hex
        idea = IdeaSnapshot.model_validate(
            {
                "work_package_id": int(unique[:8], 16),
                "lock_version": 0,
                "updated_at": "2026-07-30T00:00:00Z",
                "title": "Postgres restart",
                "description": "Exercise durable planner state.",
            }
        )
        snapshot = OpenProjectSnapshotInput.model_validate(
            {
                "captured_at": "2026-07-30T00:00:00Z",
                "etag": unique,
                "sha256": "b" * 64,
            }
        )
        repository_file = RepositoryFile(
            path="README.md",
            sha256=hashlib.sha256(content.encode()).hexdigest(),
            content=content,
        )
        request = StartPlanRequest.model_validate(
            {
                "event": {
                    "idempotency_key": f"postgres:start:{unique}",
                    "trace_id": str(uuid4()),
                },
                "idea": idea.model_dump(mode="json"),
                "plan_id": f"postgres-{unique[:12]}",
                "plan_version": 1,
                "idea_sha256": idea_snapshot_digest(idea, snapshot),
                "openproject_snapshot": snapshot.model_dump(mode="json"),
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
        )
        model = DeterministicPlanningModel()
        first = PlannerService(
            build_planner_graph(model, checkpointer),
            repository,
            PostgresExecutionGuard(DATABASE_URL, model),
        )
        created, replayed = await first.start(request)
        assert not replayed
        restarted = PlannerService(
            build_planner_graph(model, checkpointer),
            repository,
            PostgresExecutionGuard(DATABASE_URL, model),
        )
        replay, replayed = await restarted.start(request)
        assert replayed
        assert replay == created
        assert await restarted.get(created.thread_id) == created
        assert await repository.ready()
    finally:
        await pool.close()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
async def test_postgres_execution_guard_fences_stale_writer_then_recovers() -> None:
    assert DATABASE_URL is not None
    await migrate(DATABASE_URL)
    pool = postgres_pool(DATABASE_URL)
    await pool.open()
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

    unique = uuid4().hex
    request = postgres_request(unique)
    repository = PostgresIdempotencyRepository(pool)
    model = StaleOwnerModel()
    read_graph = build_planner_graph(model, AsyncPostgresSaver(pool))
    first_service = PlannerService(
        read_graph,
        repository,
        PostgresExecutionGuard(DATABASE_URL, model),
    )
    restarted_service = PlannerService(
        read_graph,
        repository,
        PostgresExecutionGuard(DATABASE_URL, model),
    )
    running = asyncio.create_task(first_service.start(request))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with pool.connection() as connection:
            await connection.execute(
                """
                UPDATE planner_idempotency
                SET lease_expires_at = now() - interval '1 second'
                WHERE idempotency_key = %s
                """,
                (request.event.idempotency_key,),
            )
        running.cancel()
        await asyncio.wait_for(suppressed.wait(), timeout=5)
        with pytest.raises(ExecutionInProgress):
            await restarted_service.start(request)
        assert model.calls == 1

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=10)
        recovered, replayed = await restarted_service.start(request)
        assert replayed
        assert recovered.status == "artifacts_ready"
        assert model.calls == 1
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await pool.close()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
async def test_postgres_claim_conflict_expiry_recovery_and_token_fencing() -> None:
    assert DATABASE_URL is not None
    await migrate(DATABASE_URL)
    pool = postgres_pool(DATABASE_URL)
    await pool.open()
    key = f"postgres:claim:{uuid4().hex}"
    other_key = f"postgres:claim:{uuid4().hex}"
    thread_id = f"openproject:{int(uuid4().hex[:8], 16)}:planning:1"
    binding = ResumeBinding.model_validate(
        {
            "interrupt_id": "interrupt-one",
            "comment_id": 1,
            "comment_created_at": "2026-07-31T00:00:00Z",
        }
    )
    other_binding = ResumeBinding.model_validate(
        {
            "interrupt_id": "interrupt-one",
            "comment_id": 2,
            "comment_created_at": "2026-07-31T00:00:01Z",
        }
    )
    try:
        repository = PostgresIdempotencyRepository(pool)
        original = await repository.claim(
            kind="resume",
            key=key,
            body_hash="a" * 64,
            thread_id=thread_id,
            resume_binding=binding,
        )
        assert isinstance(original, IdempotencyClaim)
        with pytest.raises(IdempotencyInProgress):
            await repository.claim(
                kind="resume",
                key=key,
                body_hash="a" * 64,
                thread_id=thread_id,
                resume_binding=binding,
            )
        with pytest.raises(IdempotencyInProgress):
            await repository.claim(
                kind="resume",
                key=other_key,
                body_hash="b" * 64,
                thread_id=thread_id,
                resume_binding=other_binding,
            )
        with pytest.raises(IdempotencyConflict):
            await repository.claim(
                kind="resume",
                key=key,
                body_hash="a" * 64,
                thread_id=f"{thread_id[:-1]}2",
                resume_binding=binding,
            )

        async with pool.connection() as connection:
            await connection.execute(
                """
                UPDATE planner_idempotency
                SET lease_expires_at = now() - interval '1 second'
                WHERE idempotency_key = %s
                """,
                (key,),
            )
        with pytest.raises(IdempotencyConflict) as blocked:
            await repository.claim(
                kind="resume",
                key=other_key,
                body_hash="b" * 64,
                thread_id=thread_id,
                resume_binding=other_binding,
            )
        assert type(blocked.value) is IdempotencyConflict
        recovered = await repository.claim(
            kind="resume",
            key=key,
            body_hash="a" * 64,
            thread_id=thread_id,
            resume_binding=binding,
        )
        assert isinstance(recovered, IdempotencyClaim)
        assert recovered.recovered
        with pytest.raises(IdempotencyConflict):
            await repository.finalize(original, {"result": "stale owner"})
        await repository.finalize(recovered, {"result": "recovered owner"})
        replay = await repository.claim(
            kind="resume",
            key=key,
            body_hash="a" * 64,
            thread_id=thread_id,
            resume_binding=binding,
        )
        assert isinstance(replay, IdempotentReplay)
        assert replay.response == {"result": "recovered owner"}
    finally:
        await pool.close()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
async def test_postgres_terminal_resume_abandonment_is_a_durable_tombstone() -> None:
    assert DATABASE_URL is not None
    await migrate(DATABASE_URL)
    pool = postgres_pool(DATABASE_URL)
    await pool.open()
    key = f"postgres:abandon:{uuid4().hex}"
    fresh_key = f"postgres:fresh:{uuid4().hex}"
    thread_id = f"openproject:{int(uuid4().hex[:8], 16)}:planning:1"
    binding = ResumeBinding.model_validate(
        {
            "interrupt_id": "interrupt-terminal",
            "comment_id": 88,
            "comment_created_at": "2026-08-03T01:42:05Z",
        }
    )
    try:
        repository = PostgresIdempotencyRepository(pool)
        claim = await repository.claim(
            kind="resume",
            key=key,
            body_hash="a" * 64,
            thread_id=thread_id,
            resume_binding=binding,
        )
        assert isinstance(claim, IdempotencyClaim)
        with pytest.raises(IdempotencyInProgress):
            await repository.abandon_resume(
                key=key,
                thread_id=thread_id,
                binding=binding,
                operator="pilot-operator",
                reason="terminal delivery",
            )
        async with pool.connection() as connection:
            await connection.execute(
                """
                UPDATE planner_idempotency
                SET lease_expires_at=now() - interval '1 second'
                WHERE idempotency_key=%s
                """,
                (key,),
            )
        await repository.abandon_resume(
            key=key,
            thread_id=thread_id,
            binding=binding,
            operator="pilot-operator",
            reason="terminal delivery",
        )
        with pytest.raises(IdempotencyConflict, match="attribution"):
            await repository.abandon_resume(
                key=key,
                thread_id=thread_id,
                binding=binding,
                operator="different-operator",
                reason="different reason",
            )
        await repository.abandon_resume(
            key=key,
            thread_id=thread_id,
            binding=binding,
            operator="pilot-operator",
            reason="terminal delivery",
        )
        with pytest.raises(IdempotencyConflict, match="terminally abandoned"):
            await repository.claim(
                kind="resume",
                key=key,
                body_hash="a" * 64,
                thread_id=thread_id,
                resume_binding=binding,
            )
        fresh_binding = ResumeBinding.model_validate(
            {
                "interrupt_id": "interrupt-terminal",
                "comment_id": 89,
                "comment_created_at": "2026-08-03T01:43:05Z",
            }
        )
        fresh = await repository.claim(
            kind="resume",
            key=fresh_key,
            body_hash="b" * 64,
            thread_id=thread_id,
            resume_binding=fresh_binding,
        )
        assert isinstance(fresh, IdempotencyClaim)
        async with pool.connection() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT state,claim_token,abandoned_at IS NOT NULL AS abandoned,
                           abandoned_by,abandonment_reason
                    FROM planner_idempotency WHERE idempotency_key=%s
                    """,
                    (key,),
                )
            ).fetchone()
        assert row == {
            "state": "abandoned",
            "claim_token": "",
            "abandoned": True,
            "abandoned_by": "pilot-operator",
            "abandonment_reason": "terminal delivery",
        }
    finally:
        await pool.close()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
async def test_postgres_v1_rows_migrate_to_completed_v2_replays() -> None:
    assert DATABASE_URL is not None
    schema = f"planner_test_{uuid4().hex}"
    admin = postgres_pool(DATABASE_URL)
    await admin.open()
    isolated: AsyncConnectionPool[Any] | None = None
    try:
        async with admin.connection() as connection:
            await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        isolated = postgres_pool(DATABASE_URL, search_path=schema)
        await isolated.open()
        key = f"legacy:{uuid4().hex}"
        async with isolated.connection() as connection:
            await connection.execute(
                """
                CREATE TABLE planner_idempotency (
                  idempotency_key text PRIMARY KEY,
                  operation_kind text NOT NULL,
                  request_sha256 text NOT NULL,
                  thread_id text NOT NULL,
                  response jsonb NOT NULL,
                  created_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            await connection.execute(
                """
                INSERT INTO planner_idempotency(
                  idempotency_key, operation_kind, request_sha256, thread_id, response
                ) VALUES (%s, 'start', %s, %s, %s)
                """,
                (key, "b" * 64, "openproject:1:planning:1", Jsonb({"legacy": True})),
            )
        await AsyncPostgresSaver(isolated).setup()
        repository = PostgresIdempotencyRepository(isolated)
        await repository.setup()
        replay = await repository.claim(
            kind="start",
            key=key,
            body_hash="b" * 64,
            thread_id="openproject:1:planning:1",
        )
        assert isinstance(replay, IdempotentReplay)
        assert replay.response == {"legacy": True}

        new_key = f"post-migration:{uuid4().hex}"
        new_claim = await repository.claim(
            kind="start",
            key=new_key,
            body_hash="c" * 64,
            thread_id="openproject:2:planning:1",
        )
        assert isinstance(new_claim, IdempotencyClaim)
        await repository.finalize(new_claim, {"post_migration": True})
        new_replay = await repository.claim(
            kind="start",
            key=new_key,
            body_hash="c" * 64,
            thread_id="openproject:2:planning:1",
        )
        assert isinstance(new_replay, IdempotentReplay)
        assert new_replay.response == {"post_migration": True}
        assert await repository.ready()
    finally:
        if isolated is not None:
            await isolated.close()
        async with admin.connection() as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize(
    "mutations",
    (
        pytest.param(
            (
                "ALTER TABLE checkpoint_migrations DROP CONSTRAINT checkpoint_migrations_pkey",
                "DELETE FROM checkpoint_migrations WHERE v = 8",
                "INSERT INTO checkpoint_migrations(v) VALUES (9)",
            ),
            id="duplicate-9-missing-8",
        ),
        pytest.param(
            (
                "ALTER TABLE checkpoint_writes ALTER COLUMN blob "
                "TYPE text USING encode(blob, 'hex')",
            ),
            id="wrong-blob-type",
        ),
        pytest.param(
            ("ALTER TABLE checkpoint_blobs DROP CONSTRAINT checkpoint_blobs_pkey",),
            id="missing-primary-key",
        ),
        pytest.param(
            (
                "ALTER TABLE checkpoints DROP CONSTRAINT checkpoints_pkey",
                "ALTER TABLE checkpoints ADD PRIMARY KEY (thread_id, checkpoint_id)",
            ),
            id="wrong-primary-key-columns",
        ),
        pytest.param(
            ("ALTER TABLE checkpoint_writes ALTER COLUMN blob DROP NOT NULL",),
            id="required-column-made-nullable",
        ),
        pytest.param(
            ("ALTER TABLE checkpoint_writes ALTER COLUMN task_path DROP DEFAULT",),
            id="missing-default",
        ),
        pytest.param(
            ("ALTER TABLE checkpoint_blobs ALTER COLUMN checkpoint_ns SET DEFAULT 'wrong'",),
            id="wrong-checkpoint-namespace-default",
        ),
        pytest.param(
            ("ALTER TABLE checkpoints ALTER COLUMN metadata SET DEFAULT '[]'::jsonb",),
            id="wrong-default",
        ),
        pytest.param(
            ("DROP TABLE planner_idempotency",),
            id="missing-planner-idempotency-table",
        ),
        pytest.param(
            ("DROP TABLE planner_resume_bindings",),
            id="missing-planner-resume-bindings-table",
        ),
        pytest.param(
            (
                "ALTER TABLE planner_resume_bindings ALTER COLUMN comment_id "
                "TYPE integer USING comment_id::integer",
            ),
            id="wrong-planner-column-type",
        ),
        pytest.param(
            ("ALTER TABLE planner_idempotency ALTER COLUMN state SET DEFAULT 'completed'",),
            id="wrong-planner-default",
        ),
        pytest.param(
            ("ALTER TABLE planner_idempotency ALTER COLUMN state DROP NOT NULL",),
            id="wrong-planner-nullability",
        ),
        pytest.param(
            (
                "ALTER TABLE planner_resume_bindings DROP CONSTRAINT planner_resume_bindings_pkey",
                "ALTER TABLE planner_resume_bindings ADD PRIMARY KEY (comment_id)",
            ),
            id="wrong-planner-primary-key",
        ),
        pytest.param(
            ("DELETE FROM planner_schema_migrations WHERE marker = 'planner-idempotency-v3'",),
            id="missing-required-planner-marker",
        ),
    ),
)
async def test_postgres_readiness_rejects_required_schema_drift(
    mutations: tuple[str, ...],
) -> None:
    assert DATABASE_URL is not None
    schema = f"planner_test_{uuid4().hex}"
    admin = postgres_pool(DATABASE_URL)
    await admin.open()
    isolated: AsyncConnectionPool[Any] | None = None
    try:
        async with admin.connection() as connection:
            await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        isolated = postgres_pool(DATABASE_URL, search_path=schema)
        await isolated.open()
        await AsyncPostgresSaver(isolated).setup()
        repository = PostgresIdempotencyRepository(isolated)
        await repository.setup()
        assert await repository.ready()

        async with isolated.connection() as connection:
            for mutation in mutations:
                await connection.execute(mutation)
        assert not await repository.ready()
    finally:
        if isolated is not None:
            await isolated.close()
        async with admin.connection() as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
        await admin.close()
