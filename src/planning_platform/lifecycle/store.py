"""Windmill-owned durable correlation state for cross-system lifecycle runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, cast

import psycopg
from psycopg.types.json import Jsonb

PlanRunState = Literal[
    "planning",
    "needs_input",
    "pr_open",
    "publishing",
    "published",
    "blocked",
    "failed",
]


@dataclass(frozen=True)
class PlanRun:
    idea_id: int
    plan_id: str
    plan_version: int
    thread_id: str
    repository: str
    base_branch: str
    artifact_prefix: str
    backlog_path: str
    snapshot_sha256: str
    snapshot_etag: str
    state: PlanRunState
    start_request_ciphertext: str = ""
    pending_resume_ciphertext: str | None = None
    planning_commit: str | None = None
    pull_request_number: int | None = None
    pull_request_url: str | None = None
    approved_commit: str | None = None
    backlog_blob_sha1: str | None = None
    backlog_sha256: str | None = None


class LifecycleStoreMismatch(ValueError):
    """A replay attempted to change an immutable lifecycle binding."""


class PostgresLifecycleStore:
    """Small workflow-state store; OpenProject remains the ticket authority."""

    _SCHEMA = "planning_lifecycle"
    _REQUIRED_COLUMNS: ClassVar[frozenset[tuple[str, str, str, bool]]] = frozenset(
        {
            ("plan_runs", "idea_id", "int8", True),
            ("plan_runs", "plan_id", "text", True),
            ("plan_runs", "plan_version", "int4", True),
            ("plan_runs", "thread_id", "text", True),
            ("plan_runs", "repository", "text", True),
            ("plan_runs", "base_branch", "text", True),
            ("plan_runs", "artifact_prefix", "text", True),
            ("plan_runs", "backlog_path", "text", True),
            ("plan_runs", "snapshot_sha256", "text", True),
            ("plan_runs", "snapshot_etag", "text", True),
            ("plan_runs", "state", "text", True),
            ("plan_runs", "start_request_ciphertext", "text", True),
            ("plan_runs", "pending_resume_ciphertext", "text", False),
            ("plan_runs", "planning_commit", "text", False),
            ("plan_runs", "pull_request_number", "int4", False),
            ("plan_runs", "pull_request_url", "text", False),
            ("plan_runs", "approved_commit", "text", False),
            ("plan_runs", "backlog_blob_sha1", "text", False),
            ("plan_runs", "backlog_sha256", "text", False),
            ("plan_runs", "created_at", "timestamptz", True),
            ("plan_runs", "updated_at", "timestamptz", True),
            ("audit", "audit_id", "int8", True),
            ("audit", "occurred_at", "timestamptz", True),
            ("audit", "event_id", "uuid", True),
            ("audit", "trace_id", "uuid", True),
            ("audit", "action", "text", True),
            ("audit", "outcome", "text", True),
            ("audit", "details", "jsonb", True),
        }
    )
    _REQUIRED_CONSTRAINTS: ClassVar[frozenset[tuple[str, str, tuple[str, ...], str]]] = frozenset(
        {
            (
                "plan_runs",
                "p",
                ("plan_id", "plan_version"),
                "PRIMARY KEY (plan_id, plan_version)",
            ),
            ("plan_runs", "u", ("thread_id",), "UNIQUE (thread_id)"),
            (
                "plan_runs",
                "u",
                ("idea_id", "plan_version"),
                "UNIQUE (idea_id, plan_version)",
            ),
            (
                "plan_runs",
                "c",
                ("state",),
                (
                    "CHECK ((state = ANY (ARRAY['planning'::text, "
                    "'needs_input'::text, 'pr_open'::text, 'publishing'::text, "
                    "'published'::text, 'blocked'::text, 'failed'::text])))"
                ),
            ),
            (
                "plan_runs",
                "c",
                ("pull_request_number", "pull_request_url"),
                (
                    "CHECK ((((pull_request_number IS NULL) AND "
                    "(pull_request_url IS NULL)) OR ((pull_request_number IS NOT NULL) "
                    "AND (pull_request_url IS NOT NULL))))"
                ),
            ),
            ("audit", "p", ("audit_id",), "PRIMARY KEY (audit_id)"),
            (
                "audit",
                "u",
                ("event_id", "action", "outcome"),
                "UNIQUE (event_id, action, outcome)",
            ),
        }
    )
    _STATES = (
        "planning",
        "needs_input",
        "pr_open",
        "publishing",
        "published",
        "blocked",
        "failed",
    )
    _SELECT = """
        idea_id, plan_id, plan_version, thread_id, repository, base_branch,
        artifact_prefix, backlog_path, snapshot_sha256, snapshot_etag, state,
        start_request_ciphertext, pending_resume_ciphertext, planning_commit,
        pull_request_number, pull_request_url, approved_commit,
        backlog_blob_sha1, backlog_sha256
    """
    _TRANSITIONS: ClassVar[dict[PlanRunState, frozenset[PlanRunState]]] = {
        "planning": frozenset({"planning", "needs_input", "pr_open", "blocked", "failed"}),
        "needs_input": frozenset({"needs_input", "planning", "pr_open", "blocked", "failed"}),
        "pr_open": frozenset({"pr_open", "publishing", "blocked", "failed"}),
        "publishing": frozenset({"publishing", "published", "blocked", "failed"}),
        "published": frozenset({"published"}),
        "blocked": frozenset({"blocked", "planning", "failed"}),
        "failed": frozenset({"failed"}),
    }

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("lifecycle database URL is required")
        self._database_url = database_url

    def setup(self) -> None:
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 1176))",
                ("planning_lifecycle:schema-migration",),
            )
            connection.execute(f"CREATE SCHEMA IF NOT EXISTS {self._SCHEMA}")
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._SCHEMA}.plan_runs (
                    idea_id bigint NOT NULL CHECK (idea_id > 0),
                    plan_id text NOT NULL,
                    plan_version integer NOT NULL CHECK (plan_version > 0),
                    thread_id text NOT NULL UNIQUE,
                    repository text NOT NULL,
                    base_branch text NOT NULL,
                    artifact_prefix text NOT NULL,
                    backlog_path text NOT NULL,
                    snapshot_sha256 text NOT NULL
                        CHECK (snapshot_sha256 ~ '^[0-9a-f]{{64}}$'),
                    snapshot_etag text NOT NULL,
                    state text NOT NULL CHECK (state IN {self._STATES}),
                    start_request_ciphertext text NOT NULL DEFAULT '',
                    pending_resume_ciphertext text,
                    planning_commit text
                        CHECK (planning_commit IS NULL OR planning_commit ~ '^[0-9a-f]{{40}}$'),
                    pull_request_number integer
                        CHECK (pull_request_number IS NULL OR pull_request_number > 0),
                    pull_request_url text,
                    approved_commit text
                        CHECK (approved_commit IS NULL OR approved_commit ~ '^[0-9a-f]{{40}}$'),
                    backlog_blob_sha1 text
                        CHECK (backlog_blob_sha1 IS NULL OR backlog_blob_sha1 ~ '^[0-9a-f]{{40}}$'),
                    backlog_sha256 text
                        CHECK (backlog_sha256 IS NULL OR backlog_sha256 ~ '^[0-9a-f]{{64}}$'),
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (plan_id, plan_version),
                    UNIQUE (idea_id, plan_version),
                    CHECK (
                        (pull_request_number IS NULL AND pull_request_url IS NULL)
                        OR (pull_request_number IS NOT NULL AND pull_request_url IS NOT NULL)
                    )
                )
                """
            )
            for column in (
                "start_request_ciphertext text NOT NULL DEFAULT ''",
                "pending_resume_ciphertext text",
                "approved_commit text",
                "backlog_blob_sha1 text",
                "backlog_sha256 text",
            ):
                connection.execute(
                    f"""
                    ALTER TABLE {self._SCHEMA}.plan_runs
                    ADD COLUMN IF NOT EXISTS {column}
                    """
                )
            # Pre-release development schemas briefly used plaintext crash
            # payload columns. They are intentionally not migrated or retained.
            connection.execute(
                f"""
                ALTER TABLE {self._SCHEMA}.plan_runs
                  DROP COLUMN IF EXISTS start_request_json,
                  DROP COLUMN IF EXISTS pending_resume_json
                """
            )
            connection.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS plan_runs_repository_pr
                ON {self._SCHEMA}.plan_runs(repository, pull_request_number)
                WHERE pull_request_number IS NOT NULL
                """
            )
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._SCHEMA}.audit (
                    audit_id bigserial PRIMARY KEY,
                    occurred_at timestamptz NOT NULL,
                    event_id uuid NOT NULL,
                    trace_id uuid NOT NULL,
                    action text NOT NULL,
                    outcome text NOT NULL,
                    details jsonb NOT NULL,
                    UNIQUE (event_id, action, outcome)
                )
                """
            )

    def ready(self) -> bool:
        try:
            with psycopg.connect(self._database_url) as connection:
                column_rows = connection.execute(
                    """
                    SELECT table_name, column_name, udt_name, is_nullable = 'NO'
                    FROM information_schema.columns
                    WHERE table_schema = %s
                      AND table_name IN ('plan_runs', 'audit')
                    """,
                    (self._SCHEMA,),
                ).fetchall()
                constraint_rows = connection.execute(
                    """
                    SELECT relation.relname,
                           constraint_record.contype::text,
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
                      AND relation.relname IN ('plan_runs', 'audit')
                    GROUP BY relation.relname,
                             constraint_record.oid,
                             constraint_record.contype
                    """,
                    (self._SCHEMA,),
                ).fetchall()
                index_rows = connection.execute(
                    """
                    SELECT index_relation.relname,
                           index_record.indisunique,
                           array_agg(attribute.attname ORDER BY key.ordinality),
                           pg_get_expr(index_record.indpred, index_record.indrelid)
                    FROM pg_index AS index_record
                    JOIN pg_class AS table_relation
                      ON table_relation.oid = index_record.indrelid
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = table_relation.relnamespace
                    JOIN pg_class AS index_relation
                      ON index_relation.oid = index_record.indexrelid
                    JOIN LATERAL
                      unnest(index_record.indkey::smallint[]) WITH ORDINALITY
                      AS key(attnum, ordinality)
                      ON true
                    JOIN pg_attribute AS attribute
                      ON attribute.attrelid = table_relation.oid
                     AND attribute.attnum = key.attnum
                    WHERE namespace.nspname = %s
                      AND table_relation.relname = 'plan_runs'
                    GROUP BY index_relation.relname,
                             index_record.indisunique,
                             index_record.indpred,
                             index_record.indrelid
                    """,
                    (self._SCHEMA,),
                ).fetchall()
        except psycopg.Error:
            return False
        columns = {(str(row[0]), str(row[1]), str(row[2]), bool(row[3])) for row in column_rows}
        constraints = {
            (
                str(row[0]),
                str(row[1]),
                tuple(str(column) for column in row[2]),
                " ".join(str(row[3]).split()),
            )
            for row in constraint_rows
        }
        indexes = {
            str(row[0]): (
                bool(row[1]),
                tuple(str(column) for column in row[2]),
                None if row[3] is None else str(row[3]),
            )
            for row in index_rows
        }
        repository_pr = indexes.get("plan_runs_repository_pr")
        return (
            columns >= self._REQUIRED_COLUMNS
            and not {
                ("plan_runs", "start_request_json"),
                ("plan_runs", "pending_resume_json"),
            }
            & {(table, column) for table, column, _type, _required in columns}
            and constraints >= self._REQUIRED_CONSTRAINTS
            and repository_pr is not None
            and repository_pr[0]
            and repository_pr[1] == ("repository", "pull_request_number")
            and repository_pr[2] == "(pull_request_number IS NOT NULL)"
        )

    def begin(self, run: PlanRun) -> PlanRun:
        if run.state != "planning":
            raise ValueError("a new lifecycle run must start in planning")
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            row = connection.execute(
                f"""
                INSERT INTO {self._SCHEMA}.plan_runs (
                    idea_id, plan_id, plan_version, thread_id, repository,
                    base_branch, artifact_prefix, backlog_path,
                    snapshot_sha256, snapshot_etag, state, start_request_ciphertext
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'planning',%s)
                ON CONFLICT DO NOTHING
                RETURNING {self._SELECT}
                """,
                (
                    run.idea_id,
                    run.plan_id,
                    run.plan_version,
                    run.thread_id,
                    run.repository,
                    run.base_branch,
                    run.artifact_prefix,
                    run.backlog_path,
                    run.snapshot_sha256,
                    run.snapshot_etag,
                    run.start_request_ciphertext,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    f"""
                    SELECT {self._SELECT} FROM {self._SCHEMA}.plan_runs
                    WHERE plan_id=%s AND plan_version=%s
                    """,
                    (run.plan_id, run.plan_version),
                ).fetchone()
        if row is None:
            raise RuntimeError("lifecycle run insert could not be observed")
        restored = self._restore(row)
        immutable = (
            "idea_id",
            "plan_id",
            "plan_version",
            "thread_id",
            "repository",
            "base_branch",
            "artifact_prefix",
            "backlog_path",
            "snapshot_sha256",
            "snapshot_etag",
            "start_request_ciphertext",
        )
        if any(getattr(restored, field) != getattr(run, field) for field in immutable):
            raise LifecycleStoreMismatch("plan run replay changed an immutable binding")
        return restored

    def record_pull_request(
        self,
        *,
        plan_id: str,
        plan_version: int,
        planning_commit: str,
        number: int,
        url: str,
    ) -> PlanRun:
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            row = connection.execute(
                f"""
                UPDATE {self._SCHEMA}.plan_runs
                SET planning_commit=%s, pull_request_number=%s,
                    pull_request_url=%s, state='pr_open', updated_at=now()
                WHERE plan_id=%s AND plan_version=%s
                  AND state IN ('planning','needs_input','pr_open')
                  AND (
                    planning_commit IS NULL
                    OR (
                      planning_commit=%s
                      AND pull_request_number=%s
                      AND pull_request_url=%s
                    )
                  )
                RETURNING {self._SELECT}
                """,
                (
                    planning_commit,
                    number,
                    url,
                    plan_id,
                    plan_version,
                    planning_commit,
                    number,
                    url,
                ),
            ).fetchone()
        if row is None:
            raise LifecycleStoreMismatch("planning PR conflicts with the durable run")
        return self._restore(row)

    def set_state(self, run: PlanRun, state: PlanRunState) -> PlanRun:
        allowed = self._TRANSITIONS.get(run.state)
        if allowed is None or state not in allowed:
            raise LifecycleStoreMismatch(
                f"invalid lifecycle transition from {run.state} to {state}"
            )
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            row = connection.execute(
                f"""
                UPDATE {self._SCHEMA}.plan_runs
                SET state=%s, updated_at=now()
                WHERE plan_id=%s AND plan_version=%s
                  AND state=%s
                RETURNING {self._SELECT}
                """,
                (state, run.plan_id, run.plan_version, run.state),
            ).fetchone()
        if row is None:
            current = self.get(run.plan_id, run.plan_version)
            if current is not None and current.state == state:
                return current
            raise LifecycleStoreMismatch("lifecycle run changed before its state transition")
        return self._restore(row)

    def record_pending_resume(self, run: PlanRun, request_ciphertext: str) -> PlanRun:
        if not request_ciphertext:
            raise ValueError("pending planner resume request is required")
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            row = connection.execute(
                f"""
                UPDATE {self._SCHEMA}.plan_runs
                SET pending_resume_ciphertext=%s, updated_at=now()
                WHERE plan_id=%s AND plan_version=%s
                  AND state='needs_input'
                  AND (
                    pending_resume_ciphertext IS NULL
                    OR pending_resume_ciphertext=%s
                  )
                RETURNING {self._SELECT}
                """,
                (
                    request_ciphertext,
                    run.plan_id,
                    run.plan_version,
                    request_ciphertext,
                ),
            ).fetchone()
        if row is None:
            raise LifecycleStoreMismatch(
                "pending planner resume conflicts with durable lifecycle state"
            )
        return self._restore(row)

    def clear_pending_resume(self, run: PlanRun, request_ciphertext: str) -> PlanRun:
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            row = connection.execute(
                f"""
                UPDATE {self._SCHEMA}.plan_runs
                SET pending_resume_ciphertext=NULL, updated_at=now()
                WHERE plan_id=%s AND plan_version=%s
                  AND pending_resume_ciphertext=%s
                RETURNING {self._SELECT}
                """,
                (run.plan_id, run.plan_version, request_ciphertext),
            ).fetchone()
        if row is None:
            current = self.get(run.plan_id, run.plan_version)
            if current is not None and current.pending_resume_ciphertext is None:
                return current
            raise LifecycleStoreMismatch("pending planner resume could not be cleared")
        return self._restore(row)

    def record_publication_binding(
        self,
        run: PlanRun,
        *,
        approved_commit: str,
        backlog_blob_sha1: str,
        backlog_sha256: str,
    ) -> PlanRun:
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            row = connection.execute(
                f"""
                UPDATE {self._SCHEMA}.plan_runs
                SET approved_commit=%s, backlog_blob_sha1=%s, backlog_sha256=%s,
                    updated_at=now()
                WHERE plan_id=%s AND plan_version=%s
                  AND state IN ('pr_open','publishing','published')
                  AND (
                    approved_commit IS NULL
                    OR (
                      approved_commit=%s
                      AND backlog_blob_sha1=%s
                      AND backlog_sha256=%s
                    )
                  )
                RETURNING {self._SELECT}
                """,
                (
                    approved_commit,
                    backlog_blob_sha1,
                    backlog_sha256,
                    run.plan_id,
                    run.plan_version,
                    approved_commit,
                    backlog_blob_sha1,
                    backlog_sha256,
                ),
            ).fetchone()
        if row is None:
            raise LifecycleStoreMismatch(
                "publication artifact binding conflicts with the durable run"
            )
        return self._restore(row)

    def get(self, plan_id: str, plan_version: int) -> PlanRun | None:
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                f"""
                SELECT {self._SELECT} FROM {self._SCHEMA}.plan_runs
                WHERE plan_id=%s AND plan_version=%s
                """,
                (plan_id, plan_version),
            ).fetchone()
        return None if row is None else self._restore(row)

    def by_pull_request(self, repository: str, number: int) -> PlanRun | None:
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                f"""
                SELECT {self._SELECT} FROM {self._SCHEMA}.plan_runs
                WHERE repository=%s AND pull_request_number=%s
                """,
                (repository, number),
            ).fetchone()
        return None if row is None else self._restore(row)

    def latest_for_idea(self, idea_id: int) -> PlanRun | None:
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                f"""
                SELECT {self._SELECT} FROM {self._SCHEMA}.plan_runs
                WHERE idea_id=%s
                ORDER BY plan_version DESC
                LIMIT 1
                """,
                (idea_id,),
            ).fetchone()
        return None if row is None else self._restore(row)

    def active(self) -> tuple[PlanRun, ...]:
        with psycopg.connect(self._database_url) as connection:
            rows = connection.execute(
                f"""
                SELECT {self._SELECT} FROM {self._SCHEMA}.plan_runs
                WHERE state NOT IN ('published','failed')
                ORDER BY idea_id, plan_version
                """
            ).fetchall()
        return tuple(self._restore(row) for row in rows)

    def all(self) -> tuple[PlanRun, ...]:
        with psycopg.connect(self._database_url) as connection:
            rows = connection.execute(
                f"""
                SELECT {self._SELECT} FROM {self._SCHEMA}.plan_runs
                ORDER BY idea_id, plan_version
                """
            ).fetchall()
        return tuple(self._restore(row) for row in rows)

    def audit(
        self,
        *,
        event_id: str,
        trace_id: str,
        action: str,
        outcome: str,
        details: dict[str, object],
        connection: psycopg.Connection[Any] | None = None,
    ) -> None:
        statement = f"""
            INSERT INTO {self._SCHEMA}.audit
                (occurred_at,event_id,trace_id,action,outcome,details)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (event_id,action,outcome) DO NOTHING
        """
        parameters = (
            datetime.now(UTC),
            event_id,
            trace_id,
            action,
            outcome,
            Jsonb(details),
        )
        if connection is not None:
            connection.execute(statement, parameters)
            return
        with psycopg.connect(self._database_url) as owned_connection:
            owned_connection.execute(statement, parameters)

    @staticmethod
    def _restore(row: tuple[object, ...]) -> PlanRun:
        # Column order is fixed by the explicit _SELECT projection.
        if (
            not isinstance(row[0], int)
            or not isinstance(row[2], int)
            or (row[14] is not None and not isinstance(row[14], int))
            or str(row[10]) not in PostgresLifecycleStore._STATES
        ):
            raise LifecycleStoreMismatch("lifecycle row has an invalid database shape")
        return PlanRun(
            idea_id=row[0],
            plan_id=str(row[1]),
            plan_version=row[2],
            thread_id=str(row[3]),
            repository=str(row[4]),
            base_branch=str(row[5]),
            artifact_prefix=str(row[6]),
            backlog_path=str(row[7]),
            snapshot_sha256=str(row[8]),
            snapshot_etag=str(row[9]),
            state=cast(PlanRunState, str(row[10])),
            start_request_ciphertext=str(row[11]),
            pending_resume_ciphertext=None if row[12] is None else str(row[12]),
            planning_commit=None if row[13] is None else str(row[13]),
            pull_request_number=row[14],
            pull_request_url=None if row[15] is None else str(row[15]),
            approved_commit=None if row[16] is None else str(row[16]),
            backlog_blob_sha1=None if row[17] is None else str(row[17]),
            backlog_sha256=None if row[18] is None else str(row[18]),
        )
