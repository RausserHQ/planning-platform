"""Durable, lease-based delivery deduplication for Windmill lifecycle jobs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import ClassVar, Literal, Protocol
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from .models import EventEnvelope

DeliveryState = Literal["claimed", "completed", "dead_letter"]


@dataclass(frozen=True)
class DeliveryClaim:
    acquired: bool
    state: DeliveryState
    lease_expires_at: datetime | None
    claim_token: UUID | None


class DeliveryDeduplicator(Protocol):
    def claim(self, event: EventEnvelope, *, now: datetime) -> DeliveryClaim: ...

    def renew(self, event: EventEnvelope, *, claim_token: UUID, now: datetime) -> DeliveryClaim: ...

    def complete(self, event: EventEnvelope, *, claim_token: UUID, now: datetime) -> None: ...

    def retry(
        self,
        event: EventEnvelope,
        *,
        claim_token: UUID,
        reason: str,
        now: datetime,
    ) -> None: ...

    def dead_letter(
        self,
        event: EventEnvelope,
        *,
        claim_token: UUID,
        reason: str,
        now: datetime,
    ) -> None: ...

    def restore_dead_letter(
        self,
        event: EventEnvelope,
        *,
        claim_token: UUID,
        reason: str,
        now: datetime,
    ) -> None: ...

    def recover_dead_letter(
        self,
        event: EventEnvelope,
        *,
        operator: str,
        reason: str,
        now: datetime,
    ) -> DeliveryClaim: ...

    def release_fence(self, event: EventEnvelope, *, claim_token: UUID) -> None: ...


class InMemoryDeliveryDeduplicator:
    """Useful for isolated script tests; production must use PostgreSQL."""

    def __init__(self, lease: timedelta = timedelta(minutes=10)) -> None:
        self._lease = lease
        self._deliveries: dict[str, tuple[DeliveryState, datetime | None, UUID | None]] = {}
        self._fences: dict[str, UUID] = {}

    def claim(self, event: EventEnvelope, *, now: datetime) -> DeliveryClaim:
        state = self._deliveries.get(event.idempotency_key)
        if event.idempotency_key in self._fences:
            return DeliveryClaim(
                False,
                "claimed" if state is None else state[0],
                None if state is None else state[1],
                None,
            )
        if state is not None and (state[0] != "claimed" or state[1] is None or state[1] > now):
            return DeliveryClaim(False, state[0], state[1], None)
        expires = now + self._lease
        claim_token = uuid4()
        self._deliveries[event.idempotency_key] = ("claimed", expires, claim_token)
        self._fences[event.idempotency_key] = claim_token
        return DeliveryClaim(True, "claimed", expires, claim_token)

    def renew(self, event: EventEnvelope, *, claim_token: UUID, now: datetime) -> DeliveryClaim:
        self._require_claimed(event, claim_token, now)
        expires = now + self._lease
        self._deliveries[event.idempotency_key] = ("claimed", expires, claim_token)
        return DeliveryClaim(True, "claimed", expires, claim_token)

    def complete(self, event: EventEnvelope, *, claim_token: UUID, now: datetime) -> None:
        self._require_claimed(event, claim_token, now)
        self._deliveries[event.idempotency_key] = ("completed", None, None)
        self.release_fence(event, claim_token=claim_token)

    def retry(
        self,
        event: EventEnvelope,
        *,
        claim_token: UUID,
        reason: str,
        now: datetime,
    ) -> None:
        if not reason:
            raise ValueError("retry reason is required")
        self._require_claimed(event, claim_token, now)
        self._deliveries[event.idempotency_key] = ("claimed", now, claim_token)
        self.release_fence(event, claim_token=claim_token)

    def dead_letter(
        self,
        event: EventEnvelope,
        *,
        claim_token: UUID,
        reason: str,
        now: datetime,
    ) -> None:
        if not reason:
            raise ValueError("dead-letter reason is required")
        self._require_claimed(event, claim_token, now)
        self._deliveries[event.idempotency_key] = ("dead_letter", None, None)
        self.release_fence(event, claim_token=claim_token)

    def restore_dead_letter(
        self,
        event: EventEnvelope,
        *,
        claim_token: UUID,
        reason: str,
        now: datetime,
    ) -> None:
        if not reason:
            raise ValueError("dead-letter recovery failure reason is required")
        self._require_claimed(event, claim_token, now)
        self._deliveries[event.idempotency_key] = ("dead_letter", None, None)
        self.release_fence(event, claim_token=claim_token)

    def recover_dead_letter(
        self,
        event: EventEnvelope,
        *,
        operator: str,
        reason: str,
        now: datetime,
    ) -> DeliveryClaim:
        if not operator.strip() or not reason.strip():
            raise ValueError("dead-letter recovery requires an operator and reason")
        state = self._deliveries.get(event.idempotency_key)
        if state is None or state[0] != "dead_letter":
            raise ValueError("delivery is not dead-lettered")
        if event.idempotency_key in self._fences:
            raise ValueError("delivery side-effect fence is still active")
        token = uuid4()
        expires = now + self._lease
        self._deliveries[event.idempotency_key] = ("claimed", expires, token)
        self._fences[event.idempotency_key] = token
        return DeliveryClaim(True, "claimed", expires, token)

    def release_fence(self, event: EventEnvelope, *, claim_token: UUID) -> None:
        if self._fences.get(event.idempotency_key) == claim_token:
            del self._fences[event.idempotency_key]

    def _require_claimed(self, event: EventEnvelope, claim_token: UUID, now: datetime) -> None:
        del now
        state = self._deliveries.get(event.idempotency_key)
        if (
            state is None
            or state[0] != "claimed"
            or state[2] != claim_token
            or self._fences.get(event.idempotency_key) != claim_token
        ):
            raise ValueError("delivery is not currently claimed")


class PostgresDeliveryDeduplicator:
    """PostgreSQL-backed claim/finalization seam; setup is an explicit deploy step."""

    _SCHEMA = "planning_lifecycle"
    _REQUIRED_COLUMNS: ClassVar[frozenset[tuple[str, str, bool]]] = frozenset(
        {
            ("idempotency_key", "text", True),
            ("event_id", "uuid", True),
            ("source", "text", True),
            ("source_delivery_id", "text", True),
            ("payload_sha256", "text", True),
            ("state", "text", True),
            ("lease_expires_at", "timestamptz", False),
            ("claim_token", "uuid", False),
            ("dead_letter_reason", "text", False),
            ("created_at", "timestamptz", True),
            ("updated_at", "timestamptz", True),
        }
    )
    _REQUIRED_CONSTRAINTS: ClassVar[frozenset[tuple[str, tuple[str, ...], str]]] = frozenset(
        {
            ("p", ("idempotency_key",), "PRIMARY KEY (idempotency_key)"),
            (
                "u",
                ("source", "source_delivery_id"),
                "UNIQUE (source, source_delivery_id)",
            ),
            (
                "c",
                ("state",),
                (
                    "CHECK ((state = ANY (ARRAY['claimed'::text, "
                    "'completed'::text, 'dead_letter'::text])))"
                ),
            ),
        }
    )

    def __init__(self, database_url: str, lease: timedelta = timedelta(minutes=10)) -> None:
        if lease <= timedelta():
            raise ValueError("delivery lease must be positive")
        self._database_url = database_url
        self._lease = lease
        self._fences: dict[
            UUID,
            tuple[str, psycopg.Connection[tuple[object, ...]]],
        ] = {}
        self._fence_lock = Lock()

    def setup(self) -> None:
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(hashtext('planning_lifecycle.setup'))")
            connection.execute(f"CREATE SCHEMA IF NOT EXISTS {self._SCHEMA}")
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._SCHEMA}.delivery_deduplications (
                    idempotency_key text PRIMARY KEY,
                    event_id uuid NOT NULL,
                    source text NOT NULL,
                    source_delivery_id text NOT NULL,
                    payload_sha256 text NOT NULL,
                    state text NOT NULL CHECK (state IN ('claimed', 'completed', 'dead_letter')),
                    lease_expires_at timestamptz,
                    claim_token uuid,
                    dead_letter_reason text,
                    created_at timestamptz NOT NULL,
                    updated_at timestamptz NOT NULL,
                    UNIQUE (source, source_delivery_id)
                )
                """
            )
            connection.execute(
                f"""
                ALTER TABLE {self._SCHEMA}.delivery_deduplications
                ADD COLUMN IF NOT EXISTS claim_token uuid
                """
            )

    def ready(self) -> bool:
        try:
            with psycopg.connect(self._database_url) as connection:
                column_rows = connection.execute(
                    """
                    SELECT column_name, udt_name, is_nullable = 'NO'
                    FROM information_schema.columns
                    WHERE table_schema = %s
                      AND table_name = 'delivery_deduplications'
                    """,
                    (self._SCHEMA,),
                ).fetchall()
                constraint_rows = connection.execute(
                    """
                    SELECT constraint_record.contype::text,
                           COALESCE(
                               array_agg(attribute.attname ORDER BY key.ordinality)
                                   FILTER (WHERE attribute.attname IS NOT NULL),
                               ARRAY[]::name[]
                           ),
                           pg_get_constraintdef(constraint_record.oid)
                    FROM pg_constraint AS constraint_record
                    JOIN pg_class AS relation
                      ON relation.oid = constraint_record.conrelid
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    LEFT JOIN LATERAL
                      unnest(constraint_record.conkey) WITH ORDINALITY
                      AS key(attnum, ordinality)
                      ON true
                    LEFT JOIN pg_attribute AS attribute
                      ON attribute.attrelid = relation.oid
                     AND attribute.attnum = key.attnum
                    WHERE namespace.nspname = %s
                      AND relation.relname = 'delivery_deduplications'
                    GROUP BY constraint_record.oid, constraint_record.contype
                    """,
                    (self._SCHEMA,),
                ).fetchall()
        except psycopg.Error:
            return False
        columns = {(str(row[0]), str(row[1]), bool(row[2])) for row in column_rows}
        constraints = {
            (
                str(row[0]),
                tuple(str(column) for column in row[1]),
                " ".join(str(row[2]).split()),
            )
            for row in constraint_rows
        }
        return columns >= self._REQUIRED_COLUMNS and constraints >= self._REQUIRED_CONSTRAINTS

    def claim(self, event: EventEnvelope, *, now: datetime) -> DeliveryClaim:
        del now
        claim_token = uuid4()
        payload_sha = hashlib.sha256(
            event.model_dump_json(exclude={"received_at"}).encode("utf-8")
        ).hexdigest()
        if not self._acquire_fence(event, claim_token):
            with psycopg.connect(
                self._database_url,
                row_factory=dict_row,
            ) as connection:
                existing = connection.execute(
                    f"""
                    SELECT state, lease_expires_at
                    FROM {self._SCHEMA}.delivery_deduplications
                    WHERE idempotency_key = %s
                    """,
                    (event.idempotency_key,),
                ).fetchone()
            return DeliveryClaim(
                False,
                "claimed" if existing is None else existing["state"],
                None if existing is None else existing["lease_expires_at"],
                None,
            )
        keep_fence = False
        try:
            with (
                psycopg.connect(self._database_url, row_factory=dict_row) as connection,
                connection.transaction(),
            ):
                row = connection.execute(
                    f"""
                        INSERT INTO {self._SCHEMA}.delivery_deduplications
                        (idempotency_key, event_id, source, source_delivery_id, payload_sha256,
                         state, lease_expires_at, claim_token, created_at, updated_at)
                        VALUES (
                          %s, %s, %s, %s, %s, 'claimed',
                          clock_timestamp() + %s, %s, clock_timestamp(), clock_timestamp()
                        )
                        ON CONFLICT (idempotency_key) DO UPDATE
                        SET state = 'claimed',
                            lease_expires_at = clock_timestamp() + %s,
                            claim_token = EXCLUDED.claim_token,
                            updated_at = clock_timestamp(),
                            dead_letter_reason = NULL
                        WHERE {self._SCHEMA}.delivery_deduplications.state = 'claimed'
                          AND {self._SCHEMA}.delivery_deduplications.lease_expires_at
                              <= clock_timestamp()
                        RETURNING state, lease_expires_at, claim_token
                        """,
                    (
                        event.idempotency_key,
                        event.event_id,
                        event.source,
                        event.source_delivery_id,
                        payload_sha,
                        self._lease,
                        claim_token,
                        self._lease,
                    ),
                ).fetchone()
                if row is not None:
                    keep_fence = True
                    return DeliveryClaim(
                        True,
                        "claimed",
                        row["lease_expires_at"],
                        row["claim_token"],
                    )
                existing = connection.execute(
                    f"""
                        SELECT state, lease_expires_at
                        FROM {self._SCHEMA}.delivery_deduplications
                        WHERE idempotency_key = %s
                        """,
                    (event.idempotency_key,),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("delivery claim conflict could not be read")
                return DeliveryClaim(
                    False,
                    existing["state"],
                    existing["lease_expires_at"],
                    None,
                )
        finally:
            if not keep_fence:
                self.release_fence(event, claim_token=claim_token)

    def renew(self, event: EventEnvelope, *, claim_token: UUID, now: datetime) -> DeliveryClaim:
        del now
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                f"""
                UPDATE {self._SCHEMA}.delivery_deduplications
                SET lease_expires_at = clock_timestamp() + %s,
                    updated_at = clock_timestamp()
                WHERE idempotency_key = %s AND state = 'claimed'
                  AND claim_token = %s
                RETURNING state, lease_expires_at, claim_token
                """,
                (self._lease, event.idempotency_key, claim_token),
            ).fetchone()
        if row is None:
            raise ValueError("delivery claim ownership was lost")
        return DeliveryClaim(True, "claimed", row["lease_expires_at"], row["claim_token"])

    def complete(self, event: EventEnvelope, *, claim_token: UUID, now: datetime) -> None:
        self._finalize(
            event,
            claim_token=claim_token,
            state="completed",
            reason=None,
            now=now,
        )

    def retry(
        self,
        event: EventEnvelope,
        *,
        claim_token: UUID,
        reason: str,
        now: datetime,
    ) -> None:
        if not reason or len(reason) > 1_024:
            raise ValueError("retry reason must be between 1 and 1024 characters")
        del now
        try:
            with psycopg.connect(self._database_url) as connection:
                result = connection.execute(
                    f"""
                    UPDATE {self._SCHEMA}.delivery_deduplications
                    SET lease_expires_at = clock_timestamp(),
                        dead_letter_reason = %s,
                        updated_at = clock_timestamp()
                    WHERE idempotency_key = %s AND state = 'claimed'
                      AND claim_token = %s
                    """,
                    (reason, event.idempotency_key, claim_token),
                )
                if result.rowcount != 1:
                    raise ValueError("delivery is not currently claimed")
        finally:
            self.release_fence(event, claim_token=claim_token)

    def dead_letter(
        self,
        event: EventEnvelope,
        *,
        claim_token: UUID,
        reason: str,
        now: datetime,
    ) -> None:
        if not reason or len(reason) > 1_024:
            raise ValueError("dead-letter reason must be between 1 and 1024 characters")
        self._finalize(
            event,
            claim_token=claim_token,
            state="dead_letter",
            reason=reason,
            now=now,
        )

    def restore_dead_letter(
        self,
        event: EventEnvelope,
        *,
        claim_token: UUID,
        reason: str,
        now: datetime,
    ) -> None:
        if not reason or len(reason) > 1_024:
            raise ValueError(
                "dead-letter recovery failure reason must be between 1 and 1024 characters"
            )
        self._finalize(
            event,
            claim_token=claim_token,
            state="dead_letter",
            reason=reason,
            now=now,
        )

    def recover_dead_letter(
        self,
        event: EventEnvelope,
        *,
        operator: str,
        reason: str,
        now: datetime,
    ) -> DeliveryClaim:
        if not operator.strip() or len(operator) > 255:
            raise ValueError("dead-letter recovery operator is required")
        if not reason.strip() or len(reason) > 1_024:
            raise ValueError("dead-letter recovery reason is required")
        del now
        claim_token = uuid4()
        if not self._acquire_fence(event, claim_token):
            raise ValueError("delivery side-effect fence is still active")
        keep_fence = False
        try:
            with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
                row = connection.execute(
                    f"""
                    UPDATE {self._SCHEMA}.delivery_deduplications
                    SET state='claimed',
                        lease_expires_at=clock_timestamp() + %s,
                        claim_token=%s,
                        dead_letter_reason=%s,
                        updated_at=clock_timestamp()
                    WHERE idempotency_key=%s AND state='dead_letter'
                      AND event_id=%s
                    RETURNING state, lease_expires_at, claim_token
                    """,
                    (
                        self._lease,
                        claim_token,
                        f"recovered by {operator}: {reason}"[:1_024],
                        event.idempotency_key,
                        event.event_id,
                    ),
                ).fetchone()
            if row is None:
                raise ValueError("delivery is not dead-lettered")
            keep_fence = True
            return DeliveryClaim(
                True,
                "claimed",
                row["lease_expires_at"],
                row["claim_token"],
            )
        finally:
            if not keep_fence:
                self.release_fence(event, claim_token=claim_token)

    def _finalize(
        self,
        event: EventEnvelope,
        *,
        claim_token: UUID,
        state: Literal["completed", "dead_letter"],
        reason: str | None,
        now: datetime,
    ) -> None:
        del now
        try:
            with psycopg.connect(self._database_url) as connection:
                result = connection.execute(
                    f"""
                    UPDATE {self._SCHEMA}.delivery_deduplications
                    SET state = %s, lease_expires_at = NULL, claim_token = NULL,
                        dead_letter_reason = %s, updated_at = clock_timestamp()
                    WHERE idempotency_key = %s AND state = 'claimed'
                      AND claim_token = %s
                    """,
                    (state, reason, event.idempotency_key, claim_token),
                )
                if result.rowcount != 1:
                    raise ValueError("delivery is not currently claimed")
        finally:
            self.release_fence(event, claim_token=claim_token)

    def _acquire_fence(self, event: EventEnvelope, claim_token: UUID) -> bool:
        connection: psycopg.Connection[tuple[object, ...]] = psycopg.connect(
            self._database_url,
            autocommit=True,
        )
        try:
            row = connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 1176))",
                (f"planning-delivery:{event.idempotency_key}",),
            ).fetchone()
            if row is None or not bool(row[0]):
                connection.close()
                return False
            with self._fence_lock:
                self._fences[claim_token] = (event.idempotency_key, connection)
            return True
        except BaseException:
            connection.close()
            raise

    def release_fence(self, event: EventEnvelope, *, claim_token: UUID) -> None:
        with self._fence_lock:
            held = self._fences.get(claim_token)
            if held is None or held[0] != event.idempotency_key:
                return
            del self._fences[claim_token]
        held[1].close()

    def close(self) -> None:
        """Release any process-lifetime fences during runtime teardown."""
        with self._fence_lock:
            held = tuple(self._fences.values())
            self._fences.clear()
        for _idempotency_key, connection in held:
            connection.close()


def utc_now() -> datetime:
    return datetime.now(UTC)
