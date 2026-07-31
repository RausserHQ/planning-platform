# ruff: noqa: E501
"""Planning-owned durable publication intent/outcome journal.

No OpenProject credential or response body is persisted: only immutable hashes,
operation metadata, redacted outcome, and timestamps are retained.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from typing import TYPE_CHECKING, ClassVar, Protocol

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .diff import PublicationOperation

if TYPE_CHECKING:
    from .publisher import PublicationEnvelope

_IndexShape = tuple[
    str,
    bool,
    bool,
    bool,
    str,
    bool,
    bool,
    tuple[str, ...],
    bool,
]
_ConstraintSpec = tuple[str, str, str, tuple[str, ...], str]
_ConstraintShape = tuple[str, bool, tuple[str, ...], str]


class PublicationJournalMismatch(ValueError):
    pass


class AmbiguousPublicationEffect(RuntimeError):
    """A remote request may have committed but its response was lost."""


_PUBLICATION_IDENTITY = re.compile(r"^(?P<plan_id>[a-z0-9][a-z0-9-]{2,63}):v[1-9][0-9]*$")


def publication_scope(publication_identity: str) -> str:
    """Return the stable plan identity used to serialize every plan version."""
    match = _PUBLICATION_IDENTITY.fullmatch(publication_identity)
    if match is None:
        raise PublicationJournalMismatch("plan publication identity is invalid")
    return match.group("plan_id")


def envelope_hash(envelope: PublicationEnvelope) -> str:
    return hashlib.sha256(
        json.dumps(asdict(envelope), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def operation_hash(operation: PublicationOperation) -> str:
    return hashlib.sha256(
        json.dumps(asdict(operation), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _restored_operation(value: dict[str, object]) -> PublicationOperation:
    """Restore tuple-valued identities after PostgreSQL JSONB decoding."""
    restored = dict(value)
    identity = restored.get("identity")
    if isinstance(identity, list):
        restored["identity"] = tuple(identity)
    for field in ("preconditions", "payload"):
        payload = restored.get(field)
        if isinstance(payload, dict):
            restored_payload = dict(payload)
            for key in ("parent_identity", "target_identity"):
                endpoint = restored_payload.get(key)
                if isinstance(endpoint, list):
                    restored_payload[key] = tuple(endpoint)
            restored[field] = restored_payload
    return PublicationOperation(**restored)  # type: ignore[arg-type]


class PublicationJournal(Protocol):
    def resume(
        self, envelope: PublicationEnvelope
    ) -> tuple[tuple[PublicationOperation, ...], set[str]] | None: ...
    def begin(
        self, envelope: PublicationEnvelope, operations: tuple[PublicationOperation, ...]
    ) -> tuple[tuple[PublicationOperation, ...], set[str]]: ...
    def intent(self, operation: PublicationOperation) -> None: ...
    def outcome(self, operation: PublicationOperation, *, result: str = "applied") -> None: ...
    def failure(self, operation: PublicationOperation, error: Exception) -> None: ...
    def state(self, operation: PublicationOperation) -> str: ...
    def finalize(self) -> None: ...
    def ready(self) -> bool: ...
    def close(self) -> None: ...


class InMemoryPublicationJournal:
    """Explicit test-only journal; production callers must use PostgreSQL."""

    def __init__(self) -> None:
        self._runs: dict[str, tuple[str, tuple[PublicationOperation, ...], dict[str, str]]] = {}
        self._active = ""

    def resume(
        self, envelope: PublicationEnvelope
    ) -> tuple[tuple[PublicationOperation, ...], set[str]] | None:
        existing = self._runs.get(envelope.approval_event_id)
        if existing is None:
            return None
        if existing[0] != envelope_hash(envelope):
            raise PublicationJournalMismatch(
                "publication approval event has a different immutable envelope"
            )
        self._active = envelope.approval_event_id
        return existing[1], {key for key, state in existing[2].items() if state == "applied"}

    def begin(
        self, envelope: PublicationEnvelope, operations: tuple[PublicationOperation, ...]
    ) -> tuple[tuple[PublicationOperation, ...], set[str]]:
        key, digest = envelope.approval_event_id, envelope_hash(envelope)
        existing = self._runs.get(key)
        if existing is not None:
            if existing[0] != digest:
                raise PublicationJournalMismatch(
                    "publication approval event has a different immutable envelope"
                )
            self._active = key
            return existing[1], {
                op_id for op_id, state in existing[2].items() if state == "applied"
            }
        self._runs[key] = (
            digest,
            operations,
            {operation.operation_id: "planned" for operation in operations},
        )
        self._active = key
        return operations, set()

    def intent(self, operation: PublicationOperation) -> None:
        digest, operations, states = self._runs[self._active]
        if states.get(operation.operation_id) not in {"planned", "retryable"}:
            raise PublicationJournalMismatch("invalid operation state transition to intent")
        states[operation.operation_id] = "intent"
        self._runs[self._active] = digest, operations, states

    def outcome(self, operation: PublicationOperation, *, result: str = "applied") -> None:
        digest, operations, states = self._runs[self._active]
        if states.get(operation.operation_id) not in {"intent", "retryable", "ambiguous"}:
            raise PublicationJournalMismatch("invalid operation state transition to applied")
        states[operation.operation_id] = "applied"
        self._runs[self._active] = digest, operations, states

    def failure(self, operation: PublicationOperation, error: Exception) -> None:
        digest, operations, states = self._runs[self._active]
        if states.get(operation.operation_id) != "intent":
            raise PublicationJournalMismatch("invalid operation state transition to failure")
        states[operation.operation_id] = (
            "ambiguous" if isinstance(error, AmbiguousPublicationEffect) else "retryable"
        )
        self._runs[self._active] = digest, operations, states

    def state(self, operation: PublicationOperation) -> str:
        try:
            return self._runs[self._active][2][operation.operation_id]
        except KeyError as error:
            raise PublicationJournalMismatch("publication operation is not active") from error

    def finalize(self) -> None:
        return None

    def ready(self) -> bool:
        return True

    def close(self) -> None:
        self._active = ""


class PostgresPublicationJournal:
    MIGRATION_MARKER = "publication-journal-v5"
    _LEGACY_TARGET_SHA256 = "0" * 64
    _PUBLICATION_STATES = frozenset({"in_progress", "completed", "failed"})
    _OPERATION_STATES = frozenset({"planned", "intent", "ambiguous", "retryable", "applied"})
    _REQUIRED_CONSTRAINTS: ClassVar[dict[str, _ConstraintSpec]] = {
        "publications_pkey": (
            "publications",
            "p",
            "PRIMARY KEY (approval_event_id)",
            ("approval_event_id",),
            "PRIMARY KEY (approval_event_id)",
        ),
        "operations_pkey": (
            "operations",
            "p",
            "PRIMARY KEY (approval_event_id, operation_id)",
            ("approval_event_id", "operation_id"),
            "PRIMARY KEY (approval_event_id, operation_id)",
        ),
        "operations_approval_event_id_fkey": (
            "operations",
            "f",
            "FOREIGN KEY (approval_event_id) REFERENCES planning_publication.publications(approval_event_id)",
            ("approval_event_id",),
            "FOREIGN KEY (approval_event_id) REFERENCES planning_publication.publications(approval_event_id)",
        ),
        "publications_approval_event_nonempty": (
            "publications",
            "c",
            "CHECK (approval_event_id <> '')",
            ("approval_event_id",),
            "CHECK ((approval_event_id <> ''::text))",
        ),
        "publications_envelope_sha256": (
            "publications",
            "c",
            "CHECK (envelope_sha256 ~ '^[0-9a-f]{64}$')",
            ("envelope_sha256",),
            "CHECK ((envelope_sha256 ~ '^[0-9a-f]{64}$'::text))",
        ),
        "publications_operations_sha256": (
            "publications",
            "c",
            "CHECK (operations_sha256 ~ '^[0-9a-f]{64}$')",
            ("operations_sha256",),
            "CHECK ((operations_sha256 ~ '^[0-9a-f]{64}$'::text))",
        ),
        "publications_state": (
            "publications",
            "c",
            "CHECK (state IN ('in_progress','completed','failed'))",
            ("state",),
            "CHECK ((state = ANY (ARRAY['in_progress'::text, 'completed'::text, 'failed'::text])))",
        ),
        "publications_publication_identity": (
            "publications",
            "c",
            "CHECK (publication_identity <> '')",
            ("publication_identity",),
            "CHECK ((publication_identity <> ''::text))",
        ),
        "publications_legacy_archive_identity": (
            "publications",
            "c",
            "CHECK ((legacy_archive AND publication_identity LIKE 'legacy:%') OR (NOT legacy_archive AND publication_identity ~ '^[a-z0-9][a-z0-9-]{2,63}:v[1-9][0-9]*$'))",
            ("legacy_archive", "publication_identity"),
            "CHECK (((legacy_archive AND (publication_identity ~~ 'legacy:%'::text)) OR ((NOT legacy_archive) AND (publication_identity ~ '^[a-z0-9][a-z0-9-]{2,63}:v[1-9][0-9]*$'::text))))",
        ),
        "publications_target_sha256": (
            "publications",
            "c",
            "CHECK ((legacy_archive AND publication_target_sha256 = '0000000000000000000000000000000000000000000000000000000000000000') OR (NOT legacy_archive AND publication_target_sha256 ~ '^[0-9a-f]{64}$' AND publication_target_sha256 <> '0000000000000000000000000000000000000000000000000000000000000000'))",
            ("legacy_archive", "publication_target_sha256"),
            "CHECK (((legacy_archive AND (publication_target_sha256 = '0000000000000000000000000000000000000000000000000000000000000000'::text)) OR ((NOT legacy_archive) AND (publication_target_sha256 ~ '^[0-9a-f]{64}$'::text) AND (publication_target_sha256 <> '0000000000000000000000000000000000000000000000000000000000000000'::text))))",
        ),
        "operations_ordinal_nonnegative": (
            "operations",
            "c",
            "CHECK (ordinal >= 0)",
            ("ordinal",),
            "CHECK ((ordinal >= 0))",
        ),
        "operations_operation_sha256": (
            "operations",
            "c",
            "CHECK (operation_sha256 ~ '^[0-9a-f]{64}$')",
            ("operation_sha256",),
            "CHECK ((operation_sha256 ~ '^[0-9a-f]{64}$'::text))",
        ),
        "operations_document_object": (
            "operations",
            "c",
            "CHECK (jsonb_typeof(operation) = 'object')",
            ("operation",),
            "CHECK ((jsonb_typeof(operation) = 'object'::text))",
        ),
        "operations_state": (
            "operations",
            "c",
            "CHECK (state IN ('planned','intent','ambiguous','retryable','applied'))",
            ("state",),
            "CHECK ((state = ANY (ARRAY['planned'::text, 'intent'::text, 'ambiguous'::text, 'retryable'::text, 'applied'::text])))",
        ),
    }
    _REQUIRED_INDEXES: ClassVar[dict[str, _IndexShape]] = {
        "publication_operations_ordinal": (
            "operations",
            True,
            True,
            True,
            "btree",
            True,
            True,
            ("approval_event_id", "ordinal"),
            True,
        ),
        "publication_operations_pending": (
            "operations",
            False,
            True,
            True,
            "btree",
            True,
            True,
            ("approval_event_id", "state"),
            True,
        ),
        "publications_publication_identity": (
            "publications",
            False,
            True,
            True,
            "btree",
            True,
            True,
            ("publication_identity",),
            True,
        ),
    }

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("publication journal database URL is required")
        self._database_url = database_url
        self._active = ""
        self._fence: psycopg.Connection[tuple[object, ...]] | None = None

    @staticmethod
    def _constraint_shapes(
        connection: psycopg.Connection[tuple[object, ...]],
    ) -> dict[tuple[str, str], _ConstraintShape]:
        rows = connection.execute(
            """
            SELECT relation.relname,
                   constraint_record.conname,
                   constraint_record.contype,
                   constraint_record.convalidated,
                   ARRAY(
                     SELECT attribute.attname
                     FROM unnest(constraint_record.conkey) WITH ORDINALITY
                          AS constraint_column(attnum, position)
                     JOIN pg_attribute AS attribute
                       ON attribute.attrelid = constraint_record.conrelid
                      AND attribute.attnum = constraint_column.attnum
                     ORDER BY constraint_column.position
                   ),
                   pg_get_constraintdef(constraint_record.oid)
            FROM pg_constraint AS constraint_record
            JOIN pg_class AS relation
              ON relation.oid = constraint_record.conrelid
            JOIN pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'planning_publication'
            """
        ).fetchall()
        shapes: dict[tuple[str, str], _ConstraintShape] = {}
        for row in rows:
            columns = row[4]
            if not isinstance(columns, list):
                continue
            shapes[(str(row[0]), str(row[1]))] = (
                str(row[2]),
                bool(row[3]),
                tuple(str(column) for column in columns),
                " ".join(str(row[5]).split()),
            )
        return shapes

    @staticmethod
    def _index_shapes(
        connection: psycopg.Connection[tuple[object, ...]],
    ) -> dict[str, _IndexShape]:
        rows = connection.execute(
            """
            SELECT index_class.relname,
                   table_class.relname,
                   index_metadata.indisunique,
                   index_metadata.indisvalid,
                   index_metadata.indisready,
                   access_method.amname,
                   index_metadata.indpred IS NULL,
                   index_metadata.indexprs IS NULL,
                   ARRAY(
                     SELECT attribute.attname
                     FROM unnest(index_metadata.indkey) WITH ORDINALITY
                          AS key_column(attnum, position)
                     JOIN pg_attribute AS attribute
                       ON attribute.attrelid = index_metadata.indrelid
                      AND attribute.attnum = key_column.attnum
                     WHERE key_column.position <= index_metadata.indnkeyatts
                     ORDER BY key_column.position
                   ),
                   index_metadata.indnatts = index_metadata.indnkeyatts
            FROM pg_index AS index_metadata
            JOIN pg_class AS index_class
              ON index_class.oid = index_metadata.indexrelid
            JOIN pg_namespace AS index_namespace
              ON index_namespace.oid = index_class.relnamespace
            JOIN pg_class AS table_class
              ON table_class.oid = index_metadata.indrelid
            JOIN pg_namespace AS table_namespace
              ON table_namespace.oid = table_class.relnamespace
            JOIN pg_am AS access_method
              ON access_method.oid = index_class.relam
            WHERE index_namespace.nspname = 'planning_publication'
              AND table_namespace.nspname = 'planning_publication'
              AND index_class.relname = ANY(%s)
            """,
            (list(PostgresPublicationJournal._REQUIRED_INDEXES),),
        ).fetchall()
        shapes: dict[str, _IndexShape] = {}
        for row in rows:
            columns = row[8]
            if not isinstance(columns, list):
                continue
            shapes[str(row[0])] = (
                str(row[1]),
                bool(row[2]),
                bool(row[3]),
                bool(row[4]),
                str(row[5]),
                bool(row[6]),
                bool(row[7]),
                tuple(str(column) for column in columns),
                bool(row[9]),
            )
        return shapes

    def setup(self) -> None:
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            # Schema setup may be invoked by more than one Windmill worker
            # during a rollout. Serialize every DDL migration before touching
            # the shared schema so concurrent startup cannot deadlock while
            # replacing indexes or adding constraints.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 1176))",
                ("planning_publication:schema-migration",),
            )
            connection.execute("CREATE SCHEMA IF NOT EXISTS planning_publication")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS planning_publication.schema_migrations (marker text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"""
            )
            connection.execute("""CREATE TABLE IF NOT EXISTS planning_publication.publications (
              approval_event_id text PRIMARY KEY, publication_identity text NOT NULL, legacy_archive boolean NOT NULL DEFAULT false, publication_target_sha256 text NOT NULL, envelope_sha256 text NOT NULL, operations_sha256 text NOT NULL,
              state text NOT NULL DEFAULT 'in_progress' CHECK (state IN ('in_progress','completed','failed')), created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), CHECK (envelope_sha256 ~ '^[0-9a-f]{64}$'), CHECK (operations_sha256 ~ '^[0-9a-f]{64}$'))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS planning_publication.operations (
              approval_event_id text NOT NULL REFERENCES planning_publication.publications(approval_event_id), operation_id text NOT NULL,
              ordinal integer NOT NULL CHECK (ordinal >= 0), operation_sha256 text NOT NULL CHECK (operation_sha256 ~ '^[0-9a-f]{64}$'), operation jsonb NOT NULL, state text NOT NULL DEFAULT 'planned' CHECK (state IN ('planned','intent','ambiguous','retryable','applied')),
              intent_at timestamptz, outcome_at timestamptz, error_code text, result jsonb, PRIMARY KEY (approval_event_id, operation_id))""")
            connection.execute(
                "ALTER TABLE planning_publication.operations ADD COLUMN IF NOT EXISTS ordinal integer"
            )
            connection.execute(
                "ALTER TABLE planning_publication.publications ADD COLUMN IF NOT EXISTS publication_identity text"
            )
            connection.execute(
                "ALTER TABLE planning_publication.publications ADD COLUMN IF NOT EXISTS legacy_archive boolean NOT NULL DEFAULT false"
            )
            connection.execute(
                "ALTER TABLE planning_publication.publications ADD COLUMN IF NOT EXISTS publication_target_sha256 text"
            )
            constraint_shapes = self._constraint_shapes(connection)
            for name, (
                table,
                constraint_type,
                _definition,
                columns,
                expected_constraint_definition,
            ) in self._REQUIRED_CONSTRAINTS.items():
                observed = constraint_shapes.get((table, name))
                if observed is not None and observed != (
                    constraint_type,
                    True,
                    columns,
                    expected_constraint_definition,
                ):
                    raise PublicationJournalMismatch(
                        "required publication constraint does not match its contract"
                    )
            unfinished_legacy = connection.execute(
                """
                SELECT EXISTS (
                  SELECT 1
                  FROM planning_publication.publications AS publication
                  WHERE (
                    publication.publication_identity IS NULL
                    OR publication.publication_identity LIKE 'legacy:%'
                    OR EXISTS (
                      SELECT 1
                      FROM planning_publication.operations AS operation
                      WHERE operation.approval_event_id = publication.approval_event_id
                        AND operation.ordinal IS NULL
                    )
                  )
                  AND (
                    publication.state <> 'completed'
                    OR EXISTS (
                      SELECT 1
                      FROM planning_publication.operations AS operation
                      WHERE operation.approval_event_id = publication.approval_event_id
                        AND operation.state <> 'applied'
                    )
                  )
                )
                """
            ).fetchone()
            if unfinished_legacy is not None and bool(unfinished_legacy[0]):
                raise PublicationJournalMismatch(
                    "unfinished legacy publication requires explicit operator resolution"
                )
            connection.execute(
                """
                UPDATE planning_publication.publications AS publication
                SET legacy_archive = true
                WHERE publication.publication_identity IS NULL
                   OR publication.publication_identity LIKE 'legacy:%'
                   OR EXISTS (
                     SELECT 1
                     FROM planning_publication.operations AS operation
                     WHERE operation.approval_event_id = publication.approval_event_id
                       AND operation.ordinal IS NULL
                   )
                """
            )
            # A pre-v3 journal did not retain an explicit ordinal. Recover a
            # deterministic display order for terminal archive rows before
            # making the column mandatory. Unfinished legacy rows were rejected
            # above because their original replay order cannot be recovered.
            connection.execute(
                """
                WITH ordered AS (
                  SELECT approval_event_id,
                         operation_id,
                         row_number() OVER (
                           PARTITION BY approval_event_id
                           ORDER BY operation_id
                         ) - 1 AS ordinal
                  FROM planning_publication.operations
                  WHERE ordinal IS NULL
                )
                UPDATE planning_publication.operations AS operation
                SET ordinal = ordered.ordinal
                FROM ordered
                WHERE operation.approval_event_id = ordered.approval_event_id
                  AND operation.operation_id = ordered.operation_id
                """
            )
            # Terminal rows predating v4 cannot participate in a new
            # plan-identity fence or replay. Give them an explicit archive
            # identity so audit inspection cannot be confused with active state.
            connection.execute(
                "UPDATE planning_publication.publications SET publication_identity='legacy:' || approval_event_id WHERE publication_identity IS NULL"
            )
            unfinished_pre_target = connection.execute(
                """
                SELECT EXISTS (
                  SELECT 1
                  FROM planning_publication.publications AS publication
                  WHERE publication.publication_target_sha256 IS NULL
                    AND (
                      publication.state <> 'completed'
                      OR EXISTS (
                        SELECT 1
                        FROM planning_publication.operations AS operation
                        WHERE operation.approval_event_id = publication.approval_event_id
                          AND operation.state <> 'applied'
                      )
                    )
                )
                """
            ).fetchone()
            if unfinished_pre_target is not None and bool(unfinished_pre_target[0]):
                raise PublicationJournalMismatch(
                    "unfinished pre-target publication requires explicit operator resolution"
                )
            lifecycle_table = connection.execute(
                "SELECT to_regclass('planning_lifecycle.plan_runs')"
            ).fetchone()
            if lifecycle_table is not None and lifecycle_table[0] is not None:
                unresolved_lifecycle = connection.execute(
                    """
                    SELECT EXISTS (
                      SELECT 1
                      FROM planning_publication.publications AS publication
                      JOIN planning_lifecycle.plan_runs AS plan_run
                        ON publication.publication_identity =
                           plan_run.plan_id || ':v' || plan_run.plan_version::text
                      WHERE publication.publication_target_sha256 IS NULL
                        AND publication.state = 'completed'
                        AND plan_run.state = 'publishing'
                    )
                    """
                ).fetchone()
                if unresolved_lifecycle is not None and bool(unresolved_lifecycle[0]):
                    raise PublicationJournalMismatch(
                        "terminal pre-target publication requires lifecycle resolution"
                    )
            # v4 envelopes did not bind their OpenProject target. Terminal rows
            # are safe audit records but cannot be authenticated for replay;
            # archive them explicitly instead of guessing or backfilling a
            # target. Unfinished rows were rejected above.
            connection.execute(
                """
                UPDATE planning_publication.publications
                SET legacy_archive = true,
                    publication_identity = 'legacy:' || approval_event_id,
                    publication_target_sha256 = %s
                WHERE publication_target_sha256 IS NULL
                """,
                (self._LEGACY_TARGET_SHA256,),
            )
            connection.execute(
                "ALTER TABLE planning_publication.publications ALTER COLUMN publication_identity SET NOT NULL"
            )
            connection.execute(
                "ALTER TABLE planning_publication.publications ALTER COLUMN publication_target_sha256 SET NOT NULL"
            )
            connection.execute(
                "ALTER TABLE planning_publication.operations ALTER COLUMN ordinal SET NOT NULL"
            )
            for name, (table, _constraint_type, definition, _columns, _expected) in (
                self._REQUIRED_CONSTRAINTS.items()
            ):
                if (table, name) not in constraint_shapes:
                    connection.execute(
                        sql.SQL("ALTER TABLE planning_publication.{} ADD CONSTRAINT {} {}").format(
                            sql.Identifier(table),
                            sql.Identifier(name),
                            sql.SQL(definition),
                        )
                    )
            final_constraint_shapes = self._constraint_shapes(connection)
            if any(
                final_constraint_shapes.get((table, name))
                != (constraint_type, True, columns, expected)
                for name, (
                    table,
                    constraint_type,
                    _definition,
                    columns,
                    expected,
                ) in self._REQUIRED_CONSTRAINTS.items()
            ):
                raise PublicationJournalMismatch(
                    "required publication constraint does not match its contract"
                )
            definitions = {
                "publication_operations_ordinal": (
                    "CREATE UNIQUE INDEX publication_operations_ordinal ON planning_publication.operations(approval_event_id, ordinal)"
                ),
                "publication_operations_pending": (
                    "CREATE INDEX publication_operations_pending ON planning_publication.operations(approval_event_id, state)"
                ),
                # The advisory fence serializes concurrent writers. Historical
                # publications remain immutable audit records, so this is not a
                # uniqueness constraint across distinct approval events.
                "publications_publication_identity": (
                    "CREATE INDEX publications_publication_identity ON planning_publication.publications(publication_identity)"
                ),
            }
            observed_indexes = self._index_shapes(connection)
            for name, expected_index in self._REQUIRED_INDEXES.items():
                if observed_indexes.get(name) == expected_index:
                    continue
                constraint_owner = connection.execute(
                    """
                    SELECT constraint_record.conname
                    FROM pg_constraint AS constraint_record
                    WHERE constraint_record.conindid = to_regclass(%s)
                    """,
                    (f"planning_publication.{name}",),
                ).fetchone()
                if constraint_owner is not None:
                    raise PublicationJournalMismatch(
                        "malformed required publication index is constraint-owned"
                    )
                connection.execute(
                    sql.SQL("DROP INDEX IF EXISTS planning_publication.{}").format(
                        sql.Identifier(name)
                    )
                )
                connection.execute(sql.SQL(definitions[name]))
            connection.execute(
                "INSERT INTO planning_publication.schema_migrations(marker) VALUES (%s) ON CONFLICT DO NOTHING",
                (self.MIGRATION_MARKER,),
            )

    def ready(self) -> bool:
        try:
            with psycopg.connect(self._database_url) as connection:
                row = connection.execute(
                    "SELECT marker FROM planning_publication.schema_migrations WHERE marker=%s",
                    (self.MIGRATION_MARKER,),
                ).fetchone()
                operation_columns = connection.execute(
                    """
                    SELECT column_name, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema='planning_publication'
                      AND table_name='operations'
                    """
                ).fetchall()
                publication_columns = connection.execute(
                    """
                    SELECT column_name, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema='planning_publication'
                      AND table_name='publications'
                    """
                ).fetchall()
                constraint_shapes = self._constraint_shapes(connection)
                index_shapes = self._index_shapes(connection)
                required_operations = {
                    "approval_event_id",
                    "operation_id",
                    "ordinal",
                    "operation_sha256",
                    "operation",
                    "state",
                    "intent_at",
                    "outcome_at",
                    "error_code",
                    "result",
                }
                required_publications = {
                    "approval_event_id",
                    "envelope_sha256",
                    "operations_sha256",
                    "publication_identity",
                    "legacy_archive",
                    "publication_target_sha256",
                    "state",
                    "created_at",
                    "updated_at",
                }
                operation_names = {str(column[0]) for column in operation_columns}
                publication_names = {str(column[0]) for column in publication_columns}
                nonnull_operations = {
                    str(column[0]) for column in operation_columns if column[1] == "NO"
                }
                nonnull_publications = {
                    str(column[0]) for column in publication_columns if column[1] == "NO"
                }
                return (
                    row is not None
                    and required_operations <= operation_names
                    and required_publications <= publication_names
                    and {
                        "approval_event_id",
                        "operation_id",
                        "ordinal",
                        "operation_sha256",
                        "operation",
                        "state",
                    }
                    <= nonnull_operations
                    and {
                        "approval_event_id",
                        "publication_identity",
                        "legacy_archive",
                        "publication_target_sha256",
                        "envelope_sha256",
                        "operations_sha256",
                        "state",
                        "created_at",
                        "updated_at",
                    }
                    <= nonnull_publications
                    and all(
                        constraint_shapes.get((table, name))
                        == (constraint_type, True, columns, expected)
                        for name, (
                            table,
                            constraint_type,
                            _definition,
                            columns,
                            expected,
                        ) in self._REQUIRED_CONSTRAINTS.items()
                    )
                    and all(
                        index_shapes.get(name) == expected
                        for name, expected in self._REQUIRED_INDEXES.items()
                    )
                )
        except psycopg.Error:
            return False

    def _acquire_fence(self, event: str, publication_identity: str) -> None:
        if self._fence is not None:
            if self._active != event:
                raise PublicationJournalMismatch(
                    "journal instance already fences another publication"
                )
            return
        connection = psycopg.connect(self._database_url, autocommit=True)
        row = connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 1176))",
            (publication_scope(publication_identity),),
        ).fetchone()
        if row is None or not bool(row[0]):
            connection.close()
            raise PublicationJournalMismatch("publication is already in progress")
        self._fence = connection
        self._active = event

    def close(self) -> None:
        if self._fence is not None:
            self._fence.close()
            self._fence = None
        self._active = ""

    def resume(
        self, envelope: PublicationEnvelope
    ) -> tuple[tuple[PublicationOperation, ...], set[str]] | None:
        if not self.ready():
            raise PublicationJournalMismatch("publication journal schema is not ready")
        event, digest = envelope.approval_event_id, envelope_hash(envelope)
        if not envelope.publication_identity:
            raise PublicationJournalMismatch("publication identity is required")
        self._acquire_fence(event, envelope.publication_identity)
        try:
            with psycopg.connect(self._database_url) as connection:
                row = connection.execute(
                    "SELECT publication_identity,envelope_sha256,operations_sha256,state,legacy_archive,publication_target_sha256 FROM planning_publication.publications WHERE approval_event_id=%s",
                    (event,),
                ).fetchone()
                if row is None:
                    return None
                if bool(row[4]):
                    raise PublicationJournalMismatch(
                        "legacy publication is archived and cannot be replayed"
                    )
                if str(row[5]) != envelope.publication_target_sha256:
                    raise PublicationJournalMismatch("OpenProject publication target changed")
                if str(row[0]) != envelope.publication_identity or str(row[1]) != digest:
                    raise PublicationJournalMismatch(
                        "publication approval event has a different immutable envelope"
                    )
                recorded = connection.execute(
                    "SELECT ordinal,operation,operation_sha256,state FROM planning_publication.operations WHERE approval_event_id=%s ORDER BY ordinal",
                    (event,),
                ).fetchall()
            if [int(value[0]) for value in recorded] != list(range(len(recorded))):
                raise PublicationJournalMismatch("stored publication ordinals are not contiguous")
            restored = tuple(_restored_operation(dict(value)) for _, value, _, _ in recorded)
            if any(
                operation_hash(operation) != str(digest)
                for operation, (_, _, digest, _) in zip(restored, recorded, strict=True)
            ):
                raise PublicationJournalMismatch("stored publication operation hash mismatch")
            aggregate = hashlib.sha256(
                "".join(operation_hash(operation) for operation in restored).encode()
            ).hexdigest()
            if aggregate != str(row[2]):
                raise PublicationJournalMismatch("stored publication aggregate hash mismatch")
            publication_state = str(row[3])
            operation_states = [str(value[3]) for value in recorded]
            if publication_state not in self._PUBLICATION_STATES or any(
                state not in self._OPERATION_STATES for state in operation_states
            ):
                raise PublicationJournalMismatch("stored publication state is invalid")
            if publication_state == "failed":
                raise PublicationJournalMismatch("publication is terminally failed")
            if publication_state == "completed" and any(
                state != "applied" for state in operation_states
            ):
                raise PublicationJournalMismatch("completed publication has incomplete operations")
            return restored, {
                operation.operation_id
                for operation, (_, _, _, state) in zip(restored, recorded, strict=True)
                if state == "applied"
            }
        except Exception:
            self.close()
            raise

    def begin(
        self, envelope: PublicationEnvelope, operations: tuple[PublicationOperation, ...]
    ) -> tuple[tuple[PublicationOperation, ...], set[str]]:
        if not self.ready():
            raise PublicationJournalMismatch("publication journal schema is not ready")
        event, digest = envelope.approval_event_id, envelope_hash(envelope)
        if not envelope.publication_identity:
            raise PublicationJournalMismatch("publication identity is required")
        self._acquire_fence(event, envelope.publication_identity)
        operations_digest = hashlib.sha256(
            "".join(operation_hash(op) for op in operations).encode()
        ).hexdigest()
        try:
            with psycopg.connect(self._database_url) as connection, connection.transaction():
                row = connection.execute(
                    "SELECT envelope_sha256 FROM planning_publication.publications WHERE approval_event_id=%s FOR UPDATE",
                    (event,),
                ).fetchone()
                if row is not None:
                    if str(row[0]) != digest:
                        raise PublicationJournalMismatch(
                            "publication approval event has a different immutable envelope"
                        )
                    return self.resume(envelope) or ((), set())
                connection.execute(
                    "INSERT INTO planning_publication.publications(approval_event_id,publication_identity,publication_target_sha256,envelope_sha256,operations_sha256) VALUES (%s,%s,%s,%s,%s)",
                    (
                        event,
                        envelope.publication_identity,
                        envelope.publication_target_sha256,
                        digest,
                        operations_digest,
                    ),
                )
                for ordinal, operation in enumerate(operations):
                    connection.execute(
                        "INSERT INTO planning_publication.operations(approval_event_id,operation_id,ordinal,operation_sha256,operation) VALUES (%s,%s,%s,%s,%s)",
                        (
                            event,
                            operation.operation_id,
                            ordinal,
                            operation_hash(operation),
                            Jsonb(asdict(operation)),
                        ),
                    )
        except Exception:
            self.close()
            raise
        return operations, set()

    def intent(self, operation: PublicationOperation) -> None:
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            cursor = connection.execute(
                "UPDATE planning_publication.operations SET state='intent', intent_at=now(), error_code=NULL WHERE approval_event_id=%s AND operation_id=%s AND state IN ('planned','retryable')",
                (self._active, operation.operation_id),
            )
            if cursor.rowcount != 1:
                raise PublicationJournalMismatch("invalid operation state transition to intent")

    def outcome(self, operation: PublicationOperation, *, result: str = "applied") -> None:
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            cursor = connection.execute(
                "UPDATE planning_publication.operations SET state='applied', outcome_at=now(), result=%s WHERE approval_event_id=%s AND operation_id=%s AND state IN ('intent','retryable','ambiguous')",
                (Jsonb({"result": result}), self._active, operation.operation_id),
            )
            if cursor.rowcount != 1:
                raise PublicationJournalMismatch("invalid operation state transition to applied")

    def failure(self, operation: PublicationOperation, error: Exception) -> None:
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            state = "ambiguous" if isinstance(error, AmbiguousPublicationEffect) else "retryable"
            cursor = connection.execute(
                "UPDATE planning_publication.operations SET state=%s, error_code=%s WHERE approval_event_id=%s AND operation_id=%s AND state='intent'",
                (state, type(error).__name__, self._active, operation.operation_id),
            )
            if cursor.rowcount != 1:
                raise PublicationJournalMismatch("invalid operation state transition to failure")

    def state(self, operation: PublicationOperation) -> str:
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                """
                SELECT state
                FROM planning_publication.operations
                WHERE approval_event_id=%s AND operation_id=%s
                """,
                (self._active, operation.operation_id),
            ).fetchone()
        if row is None or str(row[0]) not in self._OPERATION_STATES:
            raise PublicationJournalMismatch("publication operation state is unavailable")
        return str(row[0])

    def finalize(self) -> None:
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            cursor = connection.execute(
                "UPDATE planning_publication.publications SET state='completed', updated_at=now() WHERE approval_event_id=%s AND state='in_progress' AND NOT EXISTS (SELECT 1 FROM planning_publication.operations WHERE approval_event_id=%s AND state <> 'applied')",
                (self._active, self._active),
            )
            if cursor.rowcount != 1:
                terminal = connection.execute(
                    """
                    SELECT state = 'completed'
                       AND NOT EXISTS (
                         SELECT 1
                         FROM planning_publication.operations
                         WHERE approval_event_id=%s AND state <> 'applied'
                       )
                    FROM planning_publication.publications
                    WHERE approval_event_id=%s
                    """,
                    (self._active, self._active),
                ).fetchone()
                if terminal is None or not bool(terminal[0]):
                    raise PublicationJournalMismatch("publication cannot transition to completed")
        self.close()
