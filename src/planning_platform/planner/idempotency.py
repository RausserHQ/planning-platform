"""Leased planner idempotency claims with short database transactions."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .models import ResumeBinding


class IdempotencyConflict(ValueError):
    pass


class IdempotencyInProgress(IdempotencyConflict):
    def __init__(self, lease_expires_at: datetime) -> None:
        self.lease_expires_at = lease_expires_at
        super().__init__("matching request is already in progress")


@dataclass(frozen=True)
class IdempotencyClaim:
    kind: Literal["start", "resume"]
    key: str
    body_hash: str
    thread_id: str
    token: str
    lease_expires_at: datetime
    recovered: bool


@dataclass(frozen=True)
class IdempotentReplay:
    response: dict[str, Any]


ClaimResult = IdempotencyClaim | IdempotentReplay


class IdempotencyRepository(Protocol):
    @property
    def heartbeat_interval_seconds(self) -> float: ...

    async def claim(
        self,
        *,
        kind: Literal["start", "resume"],
        key: str,
        body_hash: str,
        thread_id: str,
        resume_binding: ResumeBinding | None = None,
    ) -> ClaimResult: ...

    async def finalize(self, claim: IdempotencyClaim, response: dict[str, Any]) -> None: ...

    async def renew(self, claim: IdempotencyClaim) -> datetime: ...

    async def abandon_resume(
        self,
        *,
        key: str,
        thread_id: str,
        binding: ResumeBinding,
        operator: str,
        reason: str,
    ) -> None: ...

    async def ready(self) -> bool: ...


@dataclass
class _MemoryRecord:
    kind: Literal["start", "resume"]
    body_hash: str
    thread_id: str
    state: Literal["in_progress", "completed", "abandoned"]
    token: str
    lease_expires_at: datetime
    response: dict[str, Any] | None = None
    abandoned_by: str | None = None
    abandonment_reason: str | None = None


class InMemoryIdempotencyRepository:
    def __init__(
        self,
        *,
        lease_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._records: dict[str, _MemoryRecord] = {}
        self._resume_bindings: dict[tuple[str, int], tuple[str, ResumeBinding]] = {}
        self._lock = asyncio.Lock()
        self._lease = timedelta(seconds=lease_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def heartbeat_interval_seconds(self) -> float:
        return max(0.05, self._lease.total_seconds() / 3)

    async def claim(
        self,
        *,
        kind: Literal["start", "resume"],
        key: str,
        body_hash: str,
        thread_id: str,
        resume_binding: ResumeBinding | None = None,
    ) -> ClaimResult:
        async with self._lock:
            now = self._clock()
            existing = self._records.get(key)
            if existing is not None:
                _validate_identity(existing, kind, body_hash, thread_id)
                if existing.state == "completed":
                    assert existing.response is not None
                    return IdempotentReplay(existing.response)
                if existing.state == "abandoned":
                    raise IdempotencyConflict("idempotency claim was terminally abandoned")
                if existing.lease_expires_at > now:
                    raise IdempotencyInProgress(existing.lease_expires_at)
                token = secrets.token_hex(16)
                existing.token = token
                existing.lease_expires_at = now + self._lease
                return IdempotencyClaim(
                    kind,
                    key,
                    body_hash,
                    thread_id,
                    token,
                    existing.lease_expires_at,
                    recovered=True,
                )
            thread_owner = next(
                (
                    record
                    for record in self._records.values()
                    if record.thread_id == thread_id and record.state == "in_progress"
                ),
                None,
            )
            if thread_owner is not None:
                if thread_owner.lease_expires_at > now:
                    raise IdempotencyInProgress(thread_owner.lease_expires_at)
                raise IdempotencyConflict(
                    "planning thread has an unfinished mutation owned by another idempotency key"
                )
            if kind == "start" and any(
                record.kind == "start" and record.thread_id == thread_id
                for record in self._records.values()
            ):
                raise IdempotencyConflict("planning thread was already claimed")
            self._assert_resume_binding(thread_id, body_hash, resume_binding)
            token = secrets.token_hex(16)
            expires = now + self._lease
            self._records[key] = _MemoryRecord(
                kind, body_hash, thread_id, "in_progress", token, expires
            )
            if resume_binding is not None:
                self._resume_bindings[(thread_id, resume_binding.comment_id)] = (
                    body_hash,
                    resume_binding,
                )
            return IdempotencyClaim(
                kind, key, body_hash, thread_id, token, expires, recovered=False
            )

    async def finalize(self, claim: IdempotencyClaim, response: dict[str, Any]) -> None:
        async with self._lock:
            record = self._records.get(claim.key)
            if record is None or record.state != "in_progress" or record.token != claim.token:
                raise IdempotencyConflict("idempotency claim was lost before finalize")
            record.state = "completed"
            record.response = response

    async def renew(self, claim: IdempotencyClaim) -> datetime:
        async with self._lock:
            record = self._records.get(claim.key)
            if record is None or record.state != "in_progress" or record.token != claim.token:
                raise IdempotencyConflict("idempotency claim was lost during renewal")
            record.lease_expires_at = self._clock() + self._lease
            return record.lease_expires_at

    async def abandon_resume(
        self,
        *,
        key: str,
        thread_id: str,
        binding: ResumeBinding,
        operator: str,
        reason: str,
    ) -> None:
        normalized_operator = operator.strip()
        normalized_reason = reason.strip()
        if not normalized_operator or not normalized_reason:
            raise ValueError("terminal resume abandonment requires an operator and reason")
        async with self._lock:
            record = self._records.get(key)
            if record is None or record.kind != "resume" or record.thread_id != thread_id:
                raise IdempotencyConflict("resume claim does not match the requested thread")
            existing = self._resume_bindings.get((thread_id, binding.comment_id))
            if existing is None or existing[0] != record.body_hash or existing[1] != binding:
                raise IdempotencyConflict("resume claim does not match the requested binding")
            if record.state == "completed":
                raise IdempotencyConflict("completed resume cannot be abandoned")
            if record.state == "abandoned":
                if (
                    record.abandoned_by != normalized_operator
                    or record.abandonment_reason != normalized_reason
                ):
                    raise IdempotencyConflict(
                        "abandonment attribution conflicts with durable claim"
                    )
                return
            if record.lease_expires_at > self._clock():
                raise IdempotencyInProgress(record.lease_expires_at)
            record.state = "abandoned"
            record.token = ""
            record.lease_expires_at = self._clock()
            record.abandoned_by = normalized_operator
            record.abandonment_reason = normalized_reason

    def expire_for_test(self, key: str) -> None:
        """Expire a lease without sleeping; intended for deterministic recovery tests."""
        self._records[key].lease_expires_at = self._clock() - timedelta(seconds=1)

    def _assert_resume_binding(
        self, thread_id: str, body_hash: str, binding: ResumeBinding | None
    ) -> None:
        if binding is None:
            return
        existing = self._resume_bindings.get((thread_id, binding.comment_id))
        if existing is not None and (existing[0] != body_hash or existing[1] != binding):
            raise IdempotencyConflict("comment was already bound to a different resume")

    async def ready(self) -> bool:
        return True


class PostgresIdempotencyRepository:
    MIGRATION_MARKER = "planner-idempotency-v3"
    CHECKPOINT_MIGRATIONS = tuple(range(10))
    REQUIRED_COLUMN_SPECS: ClassVar[dict[tuple[str, str], tuple[int, str, bool, str | None]]] = {
        ("checkpoint_migrations", "v"): (1, "int4", False, None),
        ("checkpoints", "thread_id"): (1, "text", False, None),
        ("checkpoints", "checkpoint_ns"): (2, "text", False, "''::text"),
        ("checkpoints", "checkpoint_id"): (3, "text", False, None),
        ("checkpoints", "parent_checkpoint_id"): (4, "text", True, None),
        ("checkpoints", "type"): (5, "text", True, None),
        ("checkpoints", "checkpoint"): (6, "jsonb", False, None),
        ("checkpoints", "metadata"): (7, "jsonb", False, "'{}'::jsonb"),
        ("checkpoint_blobs", "thread_id"): (1, "text", False, None),
        ("checkpoint_blobs", "checkpoint_ns"): (2, "text", False, "''::text"),
        ("checkpoint_blobs", "channel"): (3, "text", False, None),
        ("checkpoint_blobs", "version"): (4, "text", False, None),
        ("checkpoint_blobs", "type"): (5, "text", False, None),
        ("checkpoint_blobs", "blob"): (6, "bytea", True, None),
        ("checkpoint_writes", "thread_id"): (1, "text", False, None),
        ("checkpoint_writes", "checkpoint_ns"): (2, "text", False, "''::text"),
        ("checkpoint_writes", "checkpoint_id"): (3, "text", False, None),
        ("checkpoint_writes", "task_id"): (4, "text", False, None),
        ("checkpoint_writes", "idx"): (5, "int4", False, None),
        ("checkpoint_writes", "channel"): (6, "text", False, None),
        ("checkpoint_writes", "type"): (7, "text", True, None),
        ("checkpoint_writes", "blob"): (8, "bytea", False, None),
        ("checkpoint_writes", "task_path"): (9, "text", False, "''::text"),
        ("planner_schema_migrations", "marker"): (1, "text", False, None),
        ("planner_schema_migrations", "applied_at"): (
            2,
            "timestamptz",
            False,
            "now()",
        ),
        ("planner_idempotency", "idempotency_key"): (1, "text", False, None),
        ("planner_idempotency", "operation_kind"): (2, "text", False, None),
        ("planner_idempotency", "request_sha256"): (3, "text", False, None),
        ("planner_idempotency", "thread_id"): (4, "text", False, None),
        ("planner_idempotency", "response"): (5, "jsonb", True, None),
        ("planner_idempotency", "state"): (6, "text", False, "'in_progress'::text"),
        ("planner_idempotency", "claim_token"): (7, "text", False, "''::text"),
        ("planner_idempotency", "lease_expires_at"): (
            8,
            "timestamptz",
            False,
            "now()",
        ),
        ("planner_idempotency", "created_at"): (9, "timestamptz", False, "now()"),
        ("planner_idempotency", "updated_at"): (10, "timestamptz", False, "now()"),
        ("planner_idempotency", "abandoned_at"): (11, "timestamptz", True, None),
        ("planner_idempotency", "abandoned_by"): (12, "text", True, None),
        ("planner_idempotency", "abandonment_reason"): (13, "text", True, None),
        ("planner_resume_bindings", "thread_id"): (1, "text", False, None),
        ("planner_resume_bindings", "comment_id"): (2, "int8", False, None),
        ("planner_resume_bindings", "request_sha256"): (3, "text", False, None),
        ("planner_resume_bindings", "interrupt_id"): (4, "text", False, None),
        ("planner_resume_bindings", "comment_created_at"): (
            5,
            "timestamptz",
            False,
            None,
        ),
    }
    REQUIRED_PRIMARY_KEYS: ClassVar[dict[str, tuple[str, ...]]] = {
        "checkpoint_migrations": ("v",),
        "checkpoints": ("thread_id", "checkpoint_ns", "checkpoint_id"),
        "checkpoint_blobs": ("thread_id", "checkpoint_ns", "channel", "version"),
        "checkpoint_writes": (
            "thread_id",
            "checkpoint_ns",
            "checkpoint_id",
            "task_id",
            "idx",
        ),
        "planner_schema_migrations": ("marker",),
        "planner_idempotency": ("idempotency_key",),
        "planner_resume_bindings": ("thread_id", "comment_id"),
    }
    REQUIRED_TABLES = tuple(REQUIRED_PRIMARY_KEYS)

    def __init__(self, pool: AsyncConnectionPool[Any], *, lease_seconds: int = 30) -> None:
        self._pool = pool
        self._lease_seconds = lease_seconds

    @property
    def heartbeat_interval_seconds(self) -> float:
        return max(0.05, self._lease_seconds / 3)

    async def setup(self) -> None:
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS planner_schema_migrations (
                  marker text PRIMARY KEY,
                  applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS planner_idempotency (
                  idempotency_key text PRIMARY KEY,
                  operation_kind text NOT NULL,
                  request_sha256 text NOT NULL,
                  thread_id text NOT NULL,
                  response jsonb,
                  state text NOT NULL DEFAULT 'in_progress',
                  claim_token text NOT NULL DEFAULT '',
                  lease_expires_at timestamptz NOT NULL DEFAULT now(),
                  created_at timestamptz NOT NULL DEFAULT now(),
                  updated_at timestamptz NOT NULL DEFAULT now(),
                  abandoned_at timestamptz,
                  abandoned_by text,
                  abandonment_reason text
                )
                """
            )
            cursor = await connection.execute(
                """
                SELECT ordinal_position
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'planner_idempotency'
                  AND column_name = 'created_at'
                """
            )
            created_at_column = await cursor.fetchone()
            reorder_legacy_layout = bool(
                created_at_column and int(_column(created_at_column, 0, "ordinal_position")) == 6
            )
            # Safe forward migration from the v1 table, whose response was NOT NULL.
            await connection.execute(
                "ALTER TABLE planner_idempotency ALTER COLUMN response DROP NOT NULL"
            )
            await connection.execute(
                "ALTER TABLE planner_idempotency ADD COLUMN IF NOT EXISTS state text"
            )
            await connection.execute(
                "ALTER TABLE planner_idempotency ADD COLUMN IF NOT EXISTS claim_token text"
            )
            await connection.execute(
                """
                ALTER TABLE planner_idempotency
                ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz
                """
            )
            await connection.execute(
                """
                ALTER TABLE planner_idempotency
                ADD COLUMN IF NOT EXISTS updated_at timestamptz
                """
            )
            await connection.execute(
                """
                ALTER TABLE planner_idempotency
                  ADD COLUMN IF NOT EXISTS abandoned_at timestamptz,
                  ADD COLUMN IF NOT EXISTS abandoned_by text,
                  ADD COLUMN IF NOT EXISTS abandonment_reason text
                """
            )
            await connection.execute(
                """
                UPDATE planner_idempotency SET
                  state = COALESCE(state, 'completed'),
                  claim_token = COALESCE(claim_token, md5(idempotency_key)),
                  lease_expires_at = COALESCE(lease_expires_at, now()),
                  updated_at = COALESCE(updated_at, created_at, now())
                WHERE state IS NULL OR claim_token IS NULL
                   OR lease_expires_at IS NULL OR updated_at IS NULL
                """
            )
            for column in ("state", "claim_token", "lease_expires_at", "updated_at"):
                await connection.execute(
                    f"ALTER TABLE planner_idempotency ALTER COLUMN {column} SET NOT NULL"
                )
            await connection.execute(
                """
                ALTER TABLE planner_idempotency
                  ALTER COLUMN state SET DEFAULT 'in_progress',
                  ALTER COLUMN claim_token SET DEFAULT '',
                  ALTER COLUMN lease_expires_at SET DEFAULT now(),
                  ALTER COLUMN updated_at SET DEFAULT now()
                """
            )
            if reorder_legacy_layout:
                await self._reorder_legacy_idempotency_table(connection)
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS planner_resume_bindings (
                  thread_id text NOT NULL,
                  comment_id bigint NOT NULL,
                  request_sha256 text NOT NULL,
                  interrupt_id text NOT NULL,
                  comment_created_at timestamptz NOT NULL,
                  PRIMARY KEY (thread_id, comment_id)
                )
                """
            )
            await connection.execute(
                """
                INSERT INTO planner_schema_migrations(marker)
                VALUES (%s) ON CONFLICT DO NOTHING
                """,
                (self.MIGRATION_MARKER,),
            )

    async def _reorder_legacy_idempotency_table(self, connection: Any) -> None:
        """Canonicalize v1 column ordinals after adding the leased-claim fields."""
        await connection.execute(
            """
            CREATE TABLE planner_idempotency_v2_layout (
              idempotency_key text PRIMARY KEY,
              operation_kind text NOT NULL,
              request_sha256 text NOT NULL,
              thread_id text NOT NULL,
              response jsonb,
              state text NOT NULL DEFAULT 'in_progress',
              claim_token text NOT NULL DEFAULT '',
              lease_expires_at timestamptz NOT NULL DEFAULT now(),
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now(),
              abandoned_at timestamptz,
              abandoned_by text,
              abandonment_reason text
            )
            """
        )
        await connection.execute(
            """
            INSERT INTO planner_idempotency_v2_layout(
              idempotency_key, operation_kind, request_sha256, thread_id,
              response, state, claim_token, lease_expires_at, created_at, updated_at,
              abandoned_at, abandoned_by, abandonment_reason
            )
            SELECT idempotency_key, operation_kind, request_sha256, thread_id,
                   response, state, claim_token, lease_expires_at, created_at, updated_at,
                   abandoned_at, abandoned_by, abandonment_reason
            FROM planner_idempotency
            """
        )
        await connection.execute("DROP TABLE planner_idempotency")
        await connection.execute(
            "ALTER TABLE planner_idempotency_v2_layout RENAME TO planner_idempotency"
        )

    async def claim(
        self,
        *,
        kind: Literal["start", "resume"],
        key: str,
        body_hash: str,
        thread_id: str,
        resume_binding: ResumeBinding | None = None,
    ) -> ClaimResult:
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 1::bigint))",
                (thread_id,),
            )
            cursor = await connection.execute(
                """
                SELECT operation_kind, request_sha256, thread_id, state,
                       claim_token, lease_expires_at, response
                FROM planner_idempotency WHERE idempotency_key = %s
                FOR UPDATE
                """,
                (key,),
            )
            row = await cursor.fetchone()
            if row is not None:
                _validate_row_identity(row, kind, body_hash, thread_id)
                if _column(row, 3, "state") == "completed":
                    return IdempotentReplay(dict(_column(row, 6, "response")))
                if _column(row, 3, "state") == "abandoned":
                    raise IdempotencyConflict("idempotency claim was terminally abandoned")
                lease_expires = _datetime_column(row, 5, "lease_expires_at")
                if lease_expires > datetime.now(UTC):
                    raise IdempotencyInProgress(lease_expires)
                token = secrets.token_hex(16)
                cursor = await connection.execute(
                    """
                    UPDATE planner_idempotency
                    SET claim_token = %s,
                        lease_expires_at = now() + (%s * interval '1 second'),
                        updated_at = now()
                    WHERE idempotency_key = %s
                    RETURNING lease_expires_at
                    """,
                    (token, self._lease_seconds, key),
                )
                recovered_row = await cursor.fetchone()
                assert recovered_row is not None
                return IdempotencyClaim(
                    kind,
                    key,
                    body_hash,
                    thread_id,
                    token,
                    _datetime_column(recovered_row, 0, "lease_expires_at"),
                    recovered=True,
                )
            cursor = await connection.execute(
                """
                SELECT lease_expires_at
                FROM planner_idempotency
                WHERE thread_id = %s AND state = 'in_progress'
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE
                """,
                (thread_id,),
            )
            thread_owner = await cursor.fetchone()
            if thread_owner is not None:
                lease_expires = _datetime_column(thread_owner, 0, "lease_expires_at")
                if lease_expires > datetime.now(UTC):
                    raise IdempotencyInProgress(lease_expires)
                raise IdempotencyConflict(
                    "planning thread has an unfinished mutation owned by another idempotency key"
                )
            if kind == "start":
                cursor = await connection.execute(
                    """
                    SELECT 1 FROM planner_idempotency
                    WHERE operation_kind = 'start' AND thread_id = %s
                    """,
                    (thread_id,),
                )
                if await cursor.fetchone() is not None:
                    raise IdempotencyConflict("planning thread was already claimed")
            await self._assert_resume_binding(connection, thread_id, body_hash, resume_binding)
            token = secrets.token_hex(16)
            cursor = await connection.execute(
                """
                INSERT INTO planner_idempotency(
                  idempotency_key, operation_kind, request_sha256, thread_id,
                  response, state, claim_token, lease_expires_at
                ) VALUES (%s, %s, %s, %s, NULL, 'in_progress', %s,
                          now() + (%s * interval '1 second'))
                RETURNING lease_expires_at
                """,
                (key, kind, body_hash, thread_id, token, self._lease_seconds),
            )
            lease_row = await cursor.fetchone()
            assert lease_row is not None
            if resume_binding is not None:
                await connection.execute(
                    """
                    INSERT INTO planner_resume_bindings(
                      thread_id, comment_id, request_sha256,
                      interrupt_id, comment_created_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        thread_id,
                        resume_binding.comment_id,
                        body_hash,
                        resume_binding.interrupt_id,
                        resume_binding.comment_created_at,
                    ),
                )
            return IdempotencyClaim(
                kind,
                key,
                body_hash,
                thread_id,
                token,
                _datetime_column(lease_row, 0, "lease_expires_at"),
                recovered=False,
            )

    async def finalize(self, claim: IdempotencyClaim, response: dict[str, Any]) -> None:
        async with self._pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                """
                UPDATE planner_idempotency
                SET state = 'completed', response = %s, updated_at = now()
                WHERE idempotency_key = %s
                  AND claim_token = %s
                  AND state = 'in_progress'
                RETURNING idempotency_key
                """,
                (Jsonb(response), claim.key, claim.token),
            )
            if await cursor.fetchone() is None:
                raise IdempotencyConflict("idempotency claim was lost before finalize")

    async def renew(self, claim: IdempotencyClaim) -> datetime:
        async with self._pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                """
                UPDATE planner_idempotency
                SET lease_expires_at = now() + (%s * interval '1 second'),
                    updated_at = now()
                WHERE idempotency_key = %s
                  AND claim_token = %s
                  AND state = 'in_progress'
                RETURNING lease_expires_at
                """,
                (self._lease_seconds, claim.key, claim.token),
            )
            row = await cursor.fetchone()
            if row is None:
                raise IdempotencyConflict("idempotency claim was lost during renewal")
            return _datetime_column(row, 0, "lease_expires_at")

    async def abandon_resume(
        self,
        *,
        key: str,
        thread_id: str,
        binding: ResumeBinding,
        operator: str,
        reason: str,
    ) -> None:
        normalized_operator = operator.strip()
        normalized_reason = reason.strip()
        if not normalized_operator or len(operator) > 255:
            raise ValueError("terminal resume abandonment operator is required")
        if not normalized_reason or len(reason) > 1_024:
            raise ValueError("terminal resume abandonment reason is required")
        async with self._pool.connection() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 1::bigint))",
                (thread_id,),
            )
            cursor = await connection.execute(
                """
                SELECT operation_kind, request_sha256, thread_id, state,
                       lease_expires_at, abandoned_by, abandonment_reason
                FROM planner_idempotency
                WHERE idempotency_key = %s
                FOR UPDATE
                """,
                (key,),
            )
            row = await cursor.fetchone()
            if (
                row is None
                or _column(row, 0, "operation_kind") != "resume"
                or _column(row, 2, "thread_id") != thread_id
            ):
                raise IdempotencyConflict("resume claim does not match the requested thread")
            cursor = await connection.execute(
                """
                SELECT request_sha256, interrupt_id, comment_created_at
                FROM planner_resume_bindings
                WHERE thread_id = %s AND comment_id = %s
                """,
                (thread_id, binding.comment_id),
            )
            resume_row = await cursor.fetchone()
            if (
                resume_row is None
                or _column(resume_row, 0, "request_sha256")
                != _column(row, 1, "request_sha256")
                or _column(resume_row, 1, "interrupt_id") != binding.interrupt_id
                or _column(resume_row, 2, "comment_created_at")
                != binding.comment_created_at
            ):
                raise IdempotencyConflict("resume claim does not match the requested binding")
            state = _column(row, 3, "state")
            if state == "completed":
                raise IdempotencyConflict("completed resume cannot be abandoned")
            if state == "abandoned":
                if (
                    _column(row, 5, "abandoned_by") != normalized_operator
                    or _column(row, 6, "abandonment_reason") != normalized_reason
                ):
                    raise IdempotencyConflict(
                        "abandonment attribution conflicts with durable claim"
                    )
                return
            lease_expires = _datetime_column(row, 4, "lease_expires_at")
            if lease_expires > datetime.now(UTC):
                raise IdempotencyInProgress(lease_expires)
            cursor = await connection.execute(
                """
                UPDATE planner_idempotency
                SET state = 'abandoned', response = NULL, claim_token = '',
                    lease_expires_at = now(), updated_at = now(),
                    abandoned_at = now(), abandoned_by = %s,
                    abandonment_reason = %s
                WHERE idempotency_key = %s AND state = 'in_progress'
                RETURNING idempotency_key
                """,
                (normalized_operator, normalized_reason, key),
            )
            if await cursor.fetchone() is None:
                raise IdempotencyConflict("resume claim could not be abandoned")

    async def _assert_resume_binding(
        self,
        connection: Any,
        thread_id: str,
        body_hash: str,
        binding: ResumeBinding | None,
    ) -> None:
        if binding is None:
            return
        cursor = await connection.execute(
            """
            SELECT request_sha256, interrupt_id, comment_created_at
            FROM planner_resume_bindings
            WHERE thread_id = %s AND comment_id = %s
            """,
            (thread_id, binding.comment_id),
        )
        row = await cursor.fetchone()
        if row is not None and (
            _column(row, 0, "request_sha256") != body_hash
            or _column(row, 1, "interrupt_id") != binding.interrupt_id
            or _column(row, 2, "comment_created_at") != binding.comment_created_at
        ):
            raise IdempotencyConflict("comment was already bound to a different resume")

    async def ready(self) -> bool:
        try:
            async with self._pool.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT EXISTS(
                      SELECT 1 FROM planner_schema_migrations WHERE marker = %s
                    ) AS planner_ready
                    """,
                    (self.MIGRATION_MARKER,),
                )
                row = await cursor.fetchone()
                if row is None or not _column(row, 0, "planner_ready"):
                    return False

                cursor = await connection.execute("SELECT v FROM checkpoint_migrations ORDER BY v")
                migrations = tuple(
                    int(_column(version, 0, "v")) for version in await cursor.fetchall()
                )
                if migrations != self.CHECKPOINT_MIGRATIONS:
                    return False

                cursor = await connection.execute(
                    """
                    SELECT table_name, column_name, ordinal_position, udt_name,
                           is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = ANY(%s)
                    """,
                    (list(self.REQUIRED_TABLES),),
                )
                columns = {
                    (
                        str(_column(column, 0, "table_name")),
                        str(_column(column, 1, "column_name")),
                    ): (
                        int(_column(column, 2, "ordinal_position")),
                        str(_column(column, 3, "udt_name")),
                        _column(column, 4, "is_nullable") == "YES",
                        (
                            None
                            if _column(column, 5, "column_default") is None
                            else str(_column(column, 5, "column_default"))
                        ),
                    )
                    for column in await cursor.fetchall()
                }
                if columns != self.REQUIRED_COLUMN_SPECS:
                    return False

                cursor = await connection.execute(
                    """
                    SELECT constraints.table_name,
                           array_agg(
                             columns.column_name::text
                             ORDER BY columns.ordinal_position
                           ) AS primary_key_columns
                    FROM information_schema.table_constraints AS constraints
                    JOIN information_schema.key_column_usage AS columns
                      ON columns.constraint_schema = constraints.constraint_schema
                     AND columns.constraint_name = constraints.constraint_name
                     AND columns.table_name = constraints.table_name
                    WHERE constraints.constraint_schema = current_schema()
                      AND constraints.constraint_type = 'PRIMARY KEY'
                      AND constraints.table_name = ANY(%s)
                    GROUP BY constraints.table_name
                    """,
                    (list(self.REQUIRED_TABLES),),
                )
                primary_keys = {
                    str(_column(primary_key, 0, "table_name")): tuple(
                        _column(primary_key, 1, "primary_key_columns")
                    )
                    for primary_key in await cursor.fetchall()
                }
                return primary_keys == self.REQUIRED_PRIMARY_KEYS
        except Exception:
            return False


def _validate_identity(
    record: _MemoryRecord,
    kind: Literal["start", "resume"],
    body_hash: str,
    thread_id: str,
) -> None:
    if record.kind != kind or record.body_hash != body_hash or record.thread_id != thread_id:
        raise IdempotencyConflict("idempotency key was reused across kind, thread, or request body")


def _validate_row_identity(
    row: Any,
    kind: Literal["start", "resume"],
    body_hash: str,
    thread_id: str,
) -> None:
    if (
        _column(row, 0, "operation_kind") != kind
        or _column(row, 1, "request_sha256") != body_hash
        or _column(row, 2, "thread_id") != thread_id
    ):
        raise IdempotencyConflict("idempotency key was reused across kind, thread, or request body")


def _column(row: Any, index: int, name: str) -> Any:
    if isinstance(row, dict):
        return row[name]
    return row[index]


def _datetime_column(row: Any, index: int, name: str) -> datetime:
    value = _column(row, index, name)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return value
