"""Windmill-owned durable correlation state for cross-system lifecycle runs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, cast
from uuid import UUID

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
    approval_evidence_sha256: str | None = None
    backlog_blob_sha1: str | None = None
    backlog_sha256: str | None = None


@dataclass(frozen=True)
class TerminalDelivery:
    idempotency_key: str
    event_id: UUID
    state: str
    lease_expires_at: datetime | None
    claim_token: UUID | None


@dataclass(frozen=True)
class CompletedTerminalResumeAbandonment:
    interrupt_id: str


@dataclass(frozen=True)
class CompletedCritiqueCorrection:
    interrupt_id: str


@dataclass
class CritiqueCorrectionLock:
    """Session-scoped PostgreSQL advisory lock for one correction's side effects."""

    connection: psycopg.Connection[Any] | None

    def release(self) -> None:
        connection, self.connection = self.connection, None
        if connection is not None:
            connection.close()


@dataclass(frozen=True)
class ImplementationPrAssociation:
    repository: str
    pull_request_number: int
    plan_id: str
    node_key: str
    work_package_id: int
    pull_request_url: str
    head_sha: str
    head_observed_at: datetime
    pull_request_state: Literal["open", "closed"] = "open"
    merged_commit: str | None = None
    successful_check_sha: str | None = None


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
            ("plan_runs", "approval_evidence_sha256", "text", False),
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
            ("implementation_pr_associations", "repository", "text", True),
            (
                "implementation_pr_associations",
                "pull_request_number",
                "int4",
                True,
            ),
            ("implementation_pr_associations", "plan_id", "text", True),
            ("implementation_pr_associations", "node_key", "text", True),
            (
                "implementation_pr_associations",
                "work_package_id",
                "int8",
                True,
            ),
            (
                "implementation_pr_associations",
                "pull_request_url",
                "text",
                True,
            ),
            ("implementation_pr_associations", "head_sha", "text", True),
            (
                "implementation_pr_associations",
                "head_observed_at",
                "timestamptz",
                True,
            ),
            (
                "implementation_pr_associations",
                "pull_request_state",
                "text",
                True,
            ),
            (
                "implementation_pr_associations",
                "merged_commit",
                "text",
                False,
            ),
            (
                "implementation_pr_associations",
                "successful_check_sha",
                "text",
                False,
            ),
            (
                "implementation_pr_associations",
                "created_at",
                "timestamptz",
                True,
            ),
            (
                "implementation_pr_associations",
                "updated_at",
                "timestamptz",
                True,
            ),
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
            (
                "implementation_pr_associations",
                "p",
                ("repository", "pull_request_number"),
                "PRIMARY KEY (repository, pull_request_number)",
            ),
        }
    )
    _REQUIRED_NAMED_CHECKS: ClassVar[frozenset[tuple[str, str, tuple[str, ...], bool]]] = frozenset(
        {
            (
                "plan_runs",
                "plan_runs_approval_evidence_sha256_format",
                ("approval_evidence_sha256",),
                True,
            ),
            (
                "implementation_pr_associations",
                "implementation_pr_repository_format",
                ("repository",),
                True,
            ),
            (
                "implementation_pr_associations",
                "implementation_pr_number_positive",
                ("pull_request_number",),
                True,
            ),
            (
                "implementation_pr_associations",
                "implementation_pr_plan_id_present",
                ("plan_id",),
                True,
            ),
            (
                "implementation_pr_associations",
                "implementation_pr_node_key_present",
                ("node_key",),
                True,
            ),
            (
                "implementation_pr_associations",
                "implementation_pr_work_package_positive",
                ("work_package_id",),
                True,
            ),
            (
                "implementation_pr_associations",
                "implementation_pr_url_format",
                ("pull_request_url",),
                True,
            ),
            (
                "implementation_pr_associations",
                "implementation_pr_head_sha_format",
                ("head_sha",),
                True,
            ),
            (
                "implementation_pr_associations",
                "implementation_pr_state_valid",
                ("pull_request_state",),
                True,
            ),
            (
                "implementation_pr_associations",
                "implementation_pr_merge_sha_format",
                ("merged_commit",),
                True,
            ),
            (
                "implementation_pr_associations",
                "implementation_pr_success_sha_format",
                ("successful_check_sha",),
                True,
            ),
            (
                "implementation_pr_associations",
                "implementation_pr_success_current_head",
                ("head_sha", "successful_check_sha"),
                True,
            ),
            (
                "implementation_pr_associations",
                "implementation_pr_merge_is_closed",
                ("merged_commit", "pull_request_state"),
                True,
            ),
        }
    )
    _REQUIRED_NAMED_CHECK_DEFINITIONS: ClassVar[dict[tuple[str, str], str]] = {
        (
            "plan_runs",
            "plan_runs_approval_evidence_sha256_format",
        ): (
            "CHECK (((approval_evidence_sha256 IS NULL) OR "
            "(approval_evidence_sha256 ~ '^[0-9a-f]{64}$'::text)))"
        ),
        (
            "implementation_pr_associations",
            "implementation_pr_repository_format",
        ): "CHECK ((repository ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'::text))",
        (
            "implementation_pr_associations",
            "implementation_pr_number_positive",
        ): "CHECK ((pull_request_number > 0))",
        (
            "implementation_pr_associations",
            "implementation_pr_plan_id_present",
        ): "CHECK ((length(plan_id) > 0))",
        (
            "implementation_pr_associations",
            "implementation_pr_node_key_present",
        ): "CHECK ((length(node_key) > 0))",
        (
            "implementation_pr_associations",
            "implementation_pr_work_package_positive",
        ): "CHECK ((work_package_id > 0))",
        (
            "implementation_pr_associations",
            "implementation_pr_url_format",
        ): "CHECK ((pull_request_url ~~ 'https://github.com/%'::text))",
        (
            "implementation_pr_associations",
            "implementation_pr_head_sha_format",
        ): "CHECK ((head_sha ~ '^[0-9a-f]{40}$'::text))",
        (
            "implementation_pr_associations",
            "implementation_pr_state_valid",
        ): "CHECK ((pull_request_state = ANY (ARRAY['open'::text, 'closed'::text])))",
        (
            "implementation_pr_associations",
            "implementation_pr_merge_sha_format",
        ): ("CHECK (((merged_commit IS NULL) OR (merged_commit ~ '^[0-9a-f]{40}$'::text)))"),
        (
            "implementation_pr_associations",
            "implementation_pr_success_sha_format",
        ): (
            "CHECK (((successful_check_sha IS NULL) OR "
            "(successful_check_sha ~ '^[0-9a-f]{40}$'::text)))"
        ),
        (
            "implementation_pr_associations",
            "implementation_pr_success_current_head",
        ): ("CHECK (((successful_check_sha IS NULL) OR (successful_check_sha = head_sha)))"),
        (
            "implementation_pr_associations",
            "implementation_pr_merge_is_closed",
        ): ("CHECK (((merged_commit IS NULL) OR (pull_request_state = 'closed'::text)))"),
    }
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
        approval_evidence_sha256, backlog_blob_sha1, backlog_sha256
    """
    _IMPLEMENTATION_SELECT = """
        repository, pull_request_number, plan_id, node_key, work_package_id,
        pull_request_url, head_sha, head_observed_at, pull_request_state,
        merged_commit, successful_check_sha
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
                    approval_evidence_sha256 text,
                    backlog_blob_sha1 text
                        CHECK (backlog_blob_sha1 IS NULL OR backlog_blob_sha1 ~ '^[0-9a-f]{{40}}$'),
                    backlog_sha256 text
                        CHECK (backlog_sha256 IS NULL OR backlog_sha256 ~ '^[0-9a-f]{{64}}$'),
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (plan_id, plan_version),
                    UNIQUE (idea_id, plan_version),
                    CONSTRAINT plan_runs_approval_evidence_sha256_format
                        CHECK (
                            approval_evidence_sha256 IS NULL
                            OR approval_evidence_sha256 ~ '^[0-9a-f]{{64}}$'
                        ),
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
                "approval_evidence_sha256 text",
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
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._SCHEMA}.implementation_pr_associations (
                    repository text NOT NULL,
                    pull_request_number integer NOT NULL,
                    plan_id text NOT NULL,
                    node_key text NOT NULL,
                    work_package_id bigint NOT NULL,
                    pull_request_url text NOT NULL,
                    head_sha text NOT NULL,
                    head_observed_at timestamptz NOT NULL,
                    pull_request_state text NOT NULL DEFAULT 'open',
                    merged_commit text,
                    successful_check_sha text,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (repository, pull_request_number),
                    CONSTRAINT implementation_pr_repository_format
                        CHECK (repository ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
                    CONSTRAINT implementation_pr_number_positive
                        CHECK (pull_request_number > 0),
                    CONSTRAINT implementation_pr_plan_id_present
                        CHECK (length(plan_id) > 0),
                    CONSTRAINT implementation_pr_node_key_present
                        CHECK (length(node_key) > 0),
                    CONSTRAINT implementation_pr_work_package_positive
                        CHECK (work_package_id > 0),
                    CONSTRAINT implementation_pr_url_format
                        CHECK (pull_request_url LIKE 'https://github.com/%'),
                    CONSTRAINT implementation_pr_head_sha_format
                        CHECK (head_sha ~ '^[0-9a-f]{{40}}$'),
                    CONSTRAINT implementation_pr_state_valid
                        CHECK (pull_request_state IN ('open','closed')),
                    CONSTRAINT implementation_pr_merge_sha_format
                        CHECK (
                            merged_commit IS NULL
                            OR merged_commit ~ '^[0-9a-f]{{40}}$'
                        ),
                    CONSTRAINT implementation_pr_success_sha_format
                        CHECK (
                            successful_check_sha IS NULL
                            OR successful_check_sha ~ '^[0-9a-f]{{40}}$'
                        ),
                    CONSTRAINT implementation_pr_success_current_head
                        CHECK (
                            successful_check_sha IS NULL
                            OR successful_check_sha = head_sha
                        ),
                    CONSTRAINT implementation_pr_merge_is_closed
                        CHECK (
                            merged_commit IS NULL
                            OR pull_request_state = 'closed'
                        )
                )
                """
            )
            connection.execute(
                f"""
                ALTER TABLE {self._SCHEMA}.implementation_pr_associations
                ADD COLUMN IF NOT EXISTS pull_request_state text NOT NULL DEFAULT 'open'
                """
            )
            connection.execute(
                f"""
                UPDATE {self._SCHEMA}.implementation_pr_associations
                SET pull_request_state='closed'
                WHERE merged_commit IS NOT NULL
                  AND pull_request_state != 'closed'
                """
            )
            named_constraints = (
                (
                    "plan_runs",
                    "plan_runs_approval_evidence_sha256_format",
                    (
                        "approval_evidence_sha256 IS NULL OR "
                        "approval_evidence_sha256 ~ '^[0-9a-f]{64}$'"
                    ),
                ),
                (
                    "implementation_pr_associations",
                    "implementation_pr_repository_format",
                    "repository ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'",
                ),
                (
                    "implementation_pr_associations",
                    "implementation_pr_number_positive",
                    "pull_request_number > 0",
                ),
                (
                    "implementation_pr_associations",
                    "implementation_pr_plan_id_present",
                    "length(plan_id) > 0",
                ),
                (
                    "implementation_pr_associations",
                    "implementation_pr_node_key_present",
                    "length(node_key) > 0",
                ),
                (
                    "implementation_pr_associations",
                    "implementation_pr_work_package_positive",
                    "work_package_id > 0",
                ),
                (
                    "implementation_pr_associations",
                    "implementation_pr_url_format",
                    "pull_request_url LIKE 'https://github.com/%'",
                ),
                (
                    "implementation_pr_associations",
                    "implementation_pr_head_sha_format",
                    "head_sha ~ '^[0-9a-f]{40}$'",
                ),
                (
                    "implementation_pr_associations",
                    "implementation_pr_state_valid",
                    "pull_request_state IN ('open','closed')",
                ),
                (
                    "implementation_pr_associations",
                    "implementation_pr_merge_sha_format",
                    "merged_commit IS NULL OR merged_commit ~ '^[0-9a-f]{40}$'",
                ),
                (
                    "implementation_pr_associations",
                    "implementation_pr_success_sha_format",
                    ("successful_check_sha IS NULL OR successful_check_sha ~ '^[0-9a-f]{40}$'"),
                ),
                (
                    "implementation_pr_associations",
                    "implementation_pr_success_current_head",
                    "successful_check_sha IS NULL OR successful_check_sha = head_sha",
                ),
                (
                    "implementation_pr_associations",
                    "implementation_pr_merge_is_closed",
                    "merged_commit IS NULL OR pull_request_state = 'closed'",
                ),
            )
            for table, name, expression in named_constraints:
                exists = connection.execute(
                    """
                    SELECT 1
                    FROM pg_constraint AS constraint_record
                    JOIN pg_class AS relation
                      ON relation.oid = constraint_record.conrelid
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname=%s
                      AND relation.relname=%s
                      AND constraint_record.conname=%s
                    """,
                    (self._SCHEMA, table, name),
                ).fetchone()
                if exists is None:
                    connection.execute(
                        f"""
                        ALTER TABLE {self._SCHEMA}.{table}
                        ADD CONSTRAINT {name} CHECK ({expression})
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
                      AND table_name IN (
                        'plan_runs',
                        'audit',
                        'implementation_pr_associations'
                      )
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
                      AND relation.relname IN (
                        'plan_runs',
                        'audit',
                        'implementation_pr_associations'
                      )
                    GROUP BY relation.relname,
                             constraint_record.oid,
                             constraint_record.contype
                    """,
                    (self._SCHEMA,),
                ).fetchall()
                named_check_rows = connection.execute(
                    """
                    SELECT relation.relname,
                           constraint_record.conname,
                           COALESCE(
                               array_agg(attribute.attname ORDER BY attribute.attname)
                                   FILTER (WHERE attribute.attname IS NOT NULL),
                               ARRAY[]::name[]
                           ),
                           constraint_record.convalidated,
                           pg_get_constraintdef(constraint_record.oid)
                    FROM pg_constraint AS constraint_record
                    JOIN pg_class AS relation
                      ON relation.oid = constraint_record.conrelid
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    LEFT JOIN LATERAL
                      unnest(constraint_record.conkey) AS key(attnum)
                      ON true
                    LEFT JOIN pg_attribute AS attribute
                      ON attribute.attrelid = relation.oid
                     AND attribute.attnum = key.attnum
                    WHERE namespace.nspname = %s
                      AND constraint_record.contype = 'c'
                    GROUP BY relation.relname,
                             constraint_record.conname,
                             constraint_record.convalidated,
                             constraint_record.oid
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
        named_checks = {
            (
                str(row[0]),
                str(row[1]),
                tuple(str(column) for column in row[2]),
                bool(row[3]),
            )
            for row in named_check_rows
        }
        named_check_definitions = {
            (str(row[0]), str(row[1])): " ".join(str(row[4]).split()) for row in named_check_rows
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
            and named_checks >= self._REQUIRED_NAMED_CHECKS
            and all(
                named_check_definitions.get(identity) == definition
                for identity, definition in self._REQUIRED_NAMED_CHECK_DEFINITIONS.items()
            )
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

    def approve_for_publication(
        self,
        run: PlanRun,
        *,
        merge_commit: str,
        evidence_sha256: str,
    ) -> PlanRun:
        """Atomically bind verified GitHub approval evidence before any publication."""
        if run.state not in {"pr_open", "publishing"}:
            raise LifecycleStoreMismatch(
                f"cannot approve publication from lifecycle state {run.state}"
            )
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            row = connection.execute(
                f"""
                UPDATE {self._SCHEMA}.plan_runs
                SET state='publishing', approved_commit=%s,
                    approval_evidence_sha256=%s, updated_at=now()
                WHERE plan_id=%s AND plan_version=%s
                  AND state IN ('pr_open','publishing')
                  AND (
                    approved_commit IS NULL
                    OR approved_commit=%s
                  )
                  AND (
                    approval_evidence_sha256 IS NULL
                    OR approval_evidence_sha256=%s
                  )
                RETURNING {self._SELECT}
                """,
                (
                    merge_commit,
                    evidence_sha256,
                    run.plan_id,
                    run.plan_version,
                    merge_commit,
                    evidence_sha256,
                ),
            ).fetchone()
        if row is None:
            raise LifecycleStoreMismatch(
                "planning approval evidence conflicts with the durable run"
            )
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
                    OR approved_commit=%s
                  )
                  AND (
                    (
                      backlog_blob_sha1 IS NULL
                      AND backlog_sha256 IS NULL
                    )
                    OR (
                      backlog_blob_sha1=%s
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

    def terminal_delivery(self, idempotency_key: str) -> TerminalDelivery | None:
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                f"""
                SELECT idempotency_key,event_id,state,lease_expires_at,claim_token
                FROM {self._SCHEMA}.delivery_deduplications
                WHERE idempotency_key=%s
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return TerminalDelivery(
            idempotency_key=str(row[0]),
            event_id=UUID(str(row[1])),
            state=str(row[2]),
            lease_expires_at=row[3] if isinstance(row[3], datetime) else None,
            claim_token=None if row[4] is None else UUID(str(row[4])),
        )

    def completed_terminal_resume_abandonment(
        self,
        *,
        plan_id: str,
        plan_version: int,
        thread_id: str,
        idempotency_key: str,
        operator: str,
        reason: str,
    ) -> CompletedTerminalResumeAbandonment | None:
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                f"""
                SELECT audit.details->>'interrupt_id'
                FROM {self._SCHEMA}.plan_runs AS run
                JOIN {self._SCHEMA}.delivery_deduplications AS delivery
                  ON delivery.idempotency_key=%s
                JOIN {self._SCHEMA}.audit AS audit
                  ON audit.event_id=delivery.event_id
                 AND audit.action='terminal_resume_abandonment'
                 AND audit.outcome='restored_interrupt'
                WHERE run.plan_id=%s AND run.plan_version=%s AND run.thread_id=%s
                  AND run.state='needs_input'
                  AND run.pending_resume_ciphertext IS NULL
                  AND run.planning_commit IS NULL
                  AND run.pull_request_number IS NULL
                  AND run.pull_request_url IS NULL
                  AND delivery.state='dead_letter'
                  AND delivery.lease_expires_at IS NULL
                  AND delivery.claim_token IS NULL
                  AND audit.details->>'idempotency_key'=%s
                  AND audit.details->>'plan_id'=%s
                  AND audit.details->>'plan_version'=%s
                  AND audit.details->>'thread_id'=%s
                  AND audit.details->>'operator'=%s
                  AND audit.details->>'reason'=%s
                """,
                (
                    idempotency_key,
                    plan_id,
                    plan_version,
                    thread_id,
                    idempotency_key,
                    plan_id,
                    str(plan_version),
                    thread_id,
                    operator,
                    reason,
                ),
            ).fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            return None
        return CompletedTerminalResumeAbandonment(interrupt_id=row[0])

    def complete_terminal_resume_abandonment(
        self,
        *,
        run: PlanRun,
        request_ciphertext: str,
        idempotency_key: str,
        trace_id: str,
        interrupt_id: str,
        operator: str,
        reason: str,
    ) -> PlanRun:
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            delivery = connection.execute(
                f"""
                SELECT event_id,state,lease_expires_at,claim_token
                FROM {self._SCHEMA}.delivery_deduplications
                WHERE idempotency_key=%s
                FOR UPDATE
                """,
                (idempotency_key,),
            ).fetchone()
            if (
                delivery is None
                or str(delivery[1]) != "dead_letter"
                or delivery[2] is not None
                or delivery[3] is not None
            ):
                raise LifecycleStoreMismatch(
                    "terminal resume delivery is not dead-lettered and unfenced"
                )
            event_id = str(delivery[0])
            row = connection.execute(
                f"""
                UPDATE {self._SCHEMA}.plan_runs
                SET pending_resume_ciphertext=NULL, updated_at=now()
                WHERE plan_id=%s AND plan_version=%s AND thread_id=%s
                  AND state='needs_input'
                  AND pending_resume_ciphertext=%s
                  AND planning_commit IS NULL
                  AND pull_request_number IS NULL
                  AND pull_request_url IS NULL
                  AND approved_commit IS NULL
                  AND approval_evidence_sha256 IS NULL
                  AND backlog_blob_sha1 IS NULL
                  AND backlog_sha256 IS NULL
                RETURNING {self._SELECT}
                """,
                (
                    run.plan_id,
                    run.plan_version,
                    run.thread_id,
                    request_ciphertext,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    f"""
                    SELECT {self._SELECT}
                    FROM {self._SCHEMA}.plan_runs
                    WHERE plan_id=%s AND plan_version=%s AND thread_id=%s
                      AND state='needs_input'
                      AND pending_resume_ciphertext IS NULL
                    """,
                    (run.plan_id, run.plan_version, run.thread_id),
                ).fetchone()
                audited = connection.execute(
                    f"""
                    SELECT 1 FROM {self._SCHEMA}.audit
                    WHERE event_id=%s AND action='terminal_resume_abandonment'
                      AND outcome='restored_interrupt'
                      AND details->>'idempotency_key'=%s
                      AND details->>'operator'=%s
                      AND details->>'reason'=%s
                      AND details->>'plan_id'=%s
                      AND details->>'plan_version'=%s
                      AND details->>'thread_id'=%s
                      AND details->>'interrupt_id'=%s
                    """,
                    (
                        event_id,
                        idempotency_key,
                        operator,
                        reason,
                        run.plan_id,
                        str(run.plan_version),
                        run.thread_id,
                        interrupt_id,
                    ),
                ).fetchone()
                if row is None or audited is None:
                    raise LifecycleStoreMismatch(
                        "terminal resume abandonment conflicts with durable lifecycle state"
                    )
            self.audit(
                event_id=event_id,
                trace_id=trace_id,
                action="terminal_resume_abandonment",
                outcome="restored_interrupt",
                details={
                    "idempotency_key": idempotency_key,
                    "operator": operator,
                    "reason": reason,
                    "plan_id": run.plan_id,
                    "plan_version": run.plan_version,
                    "thread_id": run.thread_id,
                    "interrupt_id": interrupt_id,
                },
                connection=connection,
            )
        return self._restore(row)

    def acquire_critique_correction_lock(
        self, plan_id: str, plan_version: int
    ) -> CritiqueCorrectionLock:
        if not plan_id or plan_version <= 0:
            raise ValueError("critique correction plan identity is required")
        connection = psycopg.connect(self._database_url, autocommit=True)
        try:
            connection.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 1176))",
                (f"failed-critique-correction:{plan_id}:v{plan_version}",),
            )
        except BaseException:
            connection.close()
            raise
        return CritiqueCorrectionLock(connection)

    def completed_critique_correction(
        self,
        *,
        plan_id: str,
        plan_version: int,
        thread_id: str,
        idempotency_key: str,
        interrupt_id: str,
        comment_id: int,
        comment_created_at: datetime,
        operator: str,
        reason: str,
    ) -> CompletedCritiqueCorrection | None:
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                f"""
                SELECT audit.details->>'new_interrupt_id'
                FROM {self._SCHEMA}.plan_runs AS run
                JOIN {self._SCHEMA}.delivery_deduplications AS delivery
                  ON delivery.idempotency_key=%s
                JOIN {self._SCHEMA}.audit AS audit
                  ON audit.event_id=delivery.event_id
                 AND audit.action='failed_critique_correction'
                 AND audit.outcome='needs_input'
                WHERE run.plan_id=%s AND run.plan_version=%s AND run.thread_id=%s
                  AND run.state='needs_input'
                  AND run.pending_resume_ciphertext IS NULL
                  AND run.planning_commit IS NULL AND run.pull_request_number IS NULL
                  AND run.pull_request_url IS NULL AND run.approved_commit IS NULL
                  AND run.approval_evidence_sha256 IS NULL
                  AND run.backlog_blob_sha1 IS NULL AND run.backlog_sha256 IS NULL
                  AND delivery.state='completed'
                  AND delivery.lease_expires_at IS NULL
                  AND delivery.claim_token IS NULL
                  AND audit.details @> %s
                """,
                (
                    idempotency_key,
                    plan_id,
                    plan_version,
                    thread_id,
                    Jsonb(
                        {
                            "plan_id": plan_id,
                            "plan_version": plan_version,
                            "thread_id": thread_id,
                            "idempotency_key": idempotency_key,
                            "interrupt_id": interrupt_id,
                            "comment_id": comment_id,
                            "comment_created_at": comment_created_at.isoformat(),
                            "operator": operator,
                            "reason": reason,
                        }
                    ),
                ),
            ).fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            return None
        return CompletedCritiqueCorrection(interrupt_id=row[0])

    def complete_critique_correction(
        self,
        *,
        run: PlanRun,
        idempotency_key: str,
        interrupt_id: str,
        comment_id: int,
        comment_created_at: datetime,
        new_interrupt_id: str,
        operator: str,
        reason: str,
    ) -> PlanRun:
        details = {
            "plan_id": run.plan_id,
            "plan_version": run.plan_version,
            "thread_id": run.thread_id,
            "idempotency_key": idempotency_key,
            "interrupt_id": interrupt_id,
            "comment_id": comment_id,
            "comment_created_at": comment_created_at.isoformat(),
            "new_interrupt_id": new_interrupt_id,
            "operator": operator,
            "reason": reason,
        }
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            delivery = connection.execute(
                f"""
                SELECT event_id,state,lease_expires_at,claim_token
                FROM {self._SCHEMA}.delivery_deduplications
                WHERE idempotency_key=%s FOR UPDATE
                """,
                (idempotency_key,),
            ).fetchone()
            if (
                delivery is None
                or str(delivery[1]) != "completed"
                or delivery[2] is not None
                or delivery[3] is not None
            ):
                raise LifecycleStoreMismatch("critique correction delivery is not completed")
            event_id = str(delivery[0])
            row = connection.execute(
                f"""
                UPDATE {self._SCHEMA}.plan_runs
                SET state='needs_input', updated_at=now()
                WHERE plan_id=%s AND plan_version=%s AND thread_id=%s
                  AND state='failed' AND pending_resume_ciphertext IS NULL
                  AND planning_commit IS NULL AND pull_request_number IS NULL
                  AND pull_request_url IS NULL AND approved_commit IS NULL
                  AND approval_evidence_sha256 IS NULL
                  AND backlog_blob_sha1 IS NULL AND backlog_sha256 IS NULL
                RETURNING {self._SELECT}
                """,
                (run.plan_id, run.plan_version, run.thread_id),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    f"""SELECT {self._SELECT} FROM {self._SCHEMA}.plan_runs
                    WHERE plan_id=%s AND plan_version=%s AND thread_id=%s
                      AND state='needs_input'""",
                    (run.plan_id, run.plan_version, run.thread_id),
                ).fetchone()
                audited = connection.execute(
                    f"""SELECT 1 FROM {self._SCHEMA}.audit
                    WHERE event_id=%s AND action='failed_critique_correction'
                      AND outcome='needs_input' AND details=%s""",
                    (event_id, Jsonb(details)),
                ).fetchone()
                if row is None or audited is None:
                    raise LifecycleStoreMismatch(
                        "critique correction conflicts with durable lifecycle state"
                    )
            self.audit(
                event_id=event_id,
                trace_id=event_id,
                action="failed_critique_correction",
                outcome="needs_input",
                details=details,
                connection=connection,
            )
        return self._restore(row)

    def stale_thread_count(self, cutoff: datetime) -> int:
        """Count stale latest nonterminal planning threads without updating them."""
        if cutoff.tzinfo is None:
            raise ValueError("stale-thread cutoff must include a timezone")
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                f"""
                SELECT count(*)
                FROM (
                    SELECT DISTINCT ON (plan_id) state, updated_at
                    FROM {self._SCHEMA}.plan_runs
                    ORDER BY plan_id, plan_version DESC
                ) AS latest
                WHERE state NOT IN ('published','failed')
                  AND updated_at <= %s
                """,
                (cutoff.astimezone(UTC),),
            ).fetchone()
        if row is None or not isinstance(row[0], int):
            raise LifecycleStoreMismatch("stale-thread count has an invalid database shape")
        return row[0]

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

    def bind_implementation_pull_request(
        self,
        *,
        repository: str,
        number: int,
        plan_id: str,
        node_key: str,
        work_package_id: int,
        url: str,
        head_sha: str,
        observed_at: datetime,
        pull_request_state: str,
        merged_commit: str | None,
    ) -> ImplementationPrAssociation:
        """Persist an immutable PR-to-work-package identity and monotonic PR facts."""
        self._validate_implementation_values(
            repository=repository,
            number=number,
            plan_id=plan_id,
            node_key=node_key,
            work_package_id=work_package_id,
            url=url,
            head_sha=head_sha,
            observed_at=observed_at,
            pull_request_state=pull_request_state,
            merged_commit=merged_commit,
        )
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            connection.execute(
                f"""
                INSERT INTO {self._SCHEMA}.implementation_pr_associations (
                    repository, pull_request_number, plan_id, node_key,
                    work_package_id, pull_request_url, head_sha,
                    head_observed_at, pull_request_state, merged_commit
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (repository, pull_request_number) DO NOTHING
                """,
                (
                    repository,
                    number,
                    plan_id,
                    node_key,
                    work_package_id,
                    url,
                    head_sha,
                    observed_at,
                    pull_request_state,
                    merged_commit,
                ),
            )
            row = connection.execute(
                f"""
                SELECT {self._IMPLEMENTATION_SELECT}
                FROM {self._SCHEMA}.implementation_pr_associations
                WHERE repository=%s AND pull_request_number=%s
                FOR UPDATE
                """,
                (repository, number),
            ).fetchone()
            if row is None:
                raise RuntimeError("implementation PR binding could not be observed")
            current = self._restore_implementation(row)
            immutable = (
                current.plan_id == plan_id
                and current.node_key == node_key
                and current.work_package_id == work_package_id
                and current.pull_request_url == url
            )
            if not immutable:
                raise LifecycleStoreMismatch(
                    "implementation PR replay changed its work-package identity"
                )
            if (
                current.merged_commit is not None
                and merged_commit is not None
                and current.merged_commit != merged_commit
            ):
                raise LifecycleStoreMismatch("implementation PR replay changed its merge commit")
            if observed_at == current.head_observed_at and (
                head_sha != current.head_sha or pull_request_state != current.pull_request_state
            ):
                raise LifecycleStoreMismatch(
                    "implementation PR has conflicting facts at one observation time"
                )
            if current.merged_commit is not None and head_sha != current.head_sha:
                raise LifecycleStoreMismatch("merged implementation PR replay changed its head")
            next_head = current.head_sha
            next_observed = current.head_observed_at
            next_state: Literal["open", "closed"] = current.pull_request_state
            next_success = current.successful_check_sha
            if (
                current.merged_commit is None
                and merged_commit is None
                and observed_at > current.head_observed_at
                and (
                    head_sha != current.head_sha or pull_request_state != current.pull_request_state
                )
            ):
                next_head = head_sha
                next_observed = observed_at
                next_state = cast(
                    Literal["open", "closed"],
                    pull_request_state,
                )
                if next_head != current.head_sha:
                    next_success = None
            next_merge = current.merged_commit or merged_commit
            if next_merge is not None:
                next_state = "closed"
            row = connection.execute(
                f"""
                UPDATE {self._SCHEMA}.implementation_pr_associations
                SET head_sha=%s, head_observed_at=%s, pull_request_state=%s,
                    merged_commit=%s, successful_check_sha=%s, updated_at=now()
                WHERE repository=%s AND pull_request_number=%s
                RETURNING {self._IMPLEMENTATION_SELECT}
                """,
                (
                    next_head,
                    next_observed,
                    next_state,
                    next_merge,
                    next_success,
                    repository,
                    number,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("implementation PR binding update could not be observed")
        return self._restore_implementation(row)

    def record_implementation_check_result(
        self,
        repository: str,
        number: int,
        *,
        head_sha: str,
        passed: bool,
    ) -> ImplementationPrAssociation | None:
        """Converge required-check evidence for the association's current head."""
        if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise ValueError("implementation check head must be a full commit SHA")
        value = head_sha if passed else None
        with psycopg.connect(self._database_url) as connection, connection.transaction():
            row = connection.execute(
                f"""
                UPDATE {self._SCHEMA}.implementation_pr_associations
                SET successful_check_sha=%s, updated_at=now()
                WHERE repository=%s AND pull_request_number=%s
                  AND head_sha=%s
                RETURNING {self._IMPLEMENTATION_SELECT}
                """,
                (value, repository, number, head_sha),
            ).fetchone()
        return None if row is None else self._restore_implementation(row)

    def by_implementation_pull_request(
        self,
        repository: str,
        number: int,
    ) -> ImplementationPrAssociation | None:
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                f"""
                SELECT {self._IMPLEMENTATION_SELECT}
                FROM {self._SCHEMA}.implementation_pr_associations
                WHERE repository=%s AND pull_request_number=%s
                """,
                (repository, number),
            ).fetchone()
        return None if row is None else self._restore_implementation(row)

    def implementation_associations(self) -> tuple[ImplementationPrAssociation, ...]:
        with psycopg.connect(self._database_url) as connection:
            rows = connection.execute(
                f"""
                SELECT {self._IMPLEMENTATION_SELECT}
                FROM {self._SCHEMA}.implementation_pr_associations
                ORDER BY repository, pull_request_number
                """
            ).fetchall()
        return tuple(self._restore_implementation(row) for row in rows)

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

    def latest_published(self, plan_id: str) -> PlanRun | None:
        with psycopg.connect(self._database_url) as connection:
            row = connection.execute(
                f"""
                SELECT {self._SELECT} FROM {self._SCHEMA}.plan_runs
                WHERE plan_id=%s AND state='published'
                ORDER BY plan_version DESC
                LIMIT 1
                """,
                (plan_id,),
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
            approval_evidence_sha256=None if row[17] is None else str(row[17]),
            backlog_blob_sha1=None if row[18] is None else str(row[18]),
            backlog_sha256=None if row[19] is None else str(row[19]),
        )

    @staticmethod
    def _validate_implementation_values(
        *,
        repository: str,
        number: int,
        plan_id: str,
        node_key: str,
        work_package_id: int,
        url: str,
        head_sha: str,
        observed_at: datetime,
        pull_request_state: str,
        merged_commit: str | None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("implementation PR repository is invalid")
        if number <= 0 or work_package_id <= 0:
            raise ValueError("implementation PR identifiers must be positive")
        if not plan_id or not node_key:
            raise ValueError("implementation PR plan identity is required")
        if not url.startswith("https://github.com/"):
            raise ValueError("implementation PR URL must be a GitHub HTTPS URL")
        if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise ValueError("implementation PR head must be a full commit SHA")
        if merged_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", merged_commit):
            raise ValueError("implementation PR merge commit must be a full commit SHA")
        if observed_at.tzinfo is None:
            raise ValueError("implementation PR observation time must be timezone aware")
        if pull_request_state not in {"open", "closed"}:
            raise ValueError("implementation PR state must be open or closed")
        if merged_commit is not None and pull_request_state != "closed":
            raise ValueError("a merged implementation PR must be closed")

    @staticmethod
    def _restore_implementation(
        row: tuple[object, ...],
    ) -> ImplementationPrAssociation:
        if (
            not isinstance(row[1], int)
            or not isinstance(row[4], int)
            or not isinstance(row[7], datetime)
            or str(row[8]) not in {"open", "closed"}
        ):
            raise LifecycleStoreMismatch(
                "implementation PR association has an invalid database shape"
            )
        return ImplementationPrAssociation(
            repository=str(row[0]),
            pull_request_number=row[1],
            plan_id=str(row[2]),
            node_key=str(row[3]),
            work_package_id=row[4],
            pull_request_url=str(row[5]),
            head_sha=str(row[6]),
            head_observed_at=row[7],
            pull_request_state=cast(Literal["open", "closed"], str(row[8])),
            merged_commit=None if row[9] is None else str(row[9]),
            successful_check_sha=None if row[10] is None else str(row[10]),
        )
