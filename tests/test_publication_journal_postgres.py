# ruff: noqa: E501
from __future__ import annotations

import hashlib
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from planning_platform.diff import PublicationOperation, plan_diff
from planning_platform.loader import load_artifact
from planning_platform.models import with_approved_commit
from planning_platform.openproject import OpenProjectSnapshot
from planning_platform.publication_journal import (
    AmbiguousPublicationEffect,
    PostgresPublicationJournal,
    PublicationJournalMismatch,
    publication_scope,
)
from planning_platform.publisher import (
    PublicationEnvelope,
    PublicationRejected,
    publish,
)

DATABASE_URL = os.environ.get("PLANNER_TEST_DATABASE_URL")
pytestmark = pytest.mark.postgres
FIXTURE = Path(__file__).parents[1] / "evals/fixtures/single-repository/backlog.yaml"
TARGET_SHA256 = "e" * 64


def _envelope(event: str) -> PublicationEnvelope:
    plan_id = f"plan-{hashlib.sha256(event.encode()).hexdigest()[:20]}"
    return PublicationEnvelope(
        "a" * 40,
        "b" * 64,
        "c" * 40,
        event,
        "d" * 64,
        "etag",
        "trace",
        "e" * 64,
        f"{plan_id}:v1",
    )


def _operation(operation_id: str) -> PublicationOperation:
    return PublicationOperation(
        operation_id, "record_audit", ("plan", operation_id), {}, None, None, "trace", {}
    )


@contextmanager
def _temporary_database_url() -> Iterator[str]:
    """Give destructive migration fixtures an isolated PostgreSQL database."""
    assert DATABASE_URL is not None
    database_name = f"planning_platform_migration_{uuid.uuid4().hex}"
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        try:
            yield make_conninfo(DATABASE_URL, dbname=database_name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


def _publication(event: str):
    artifact = load_artifact(FIXTURE)
    reference = artifact.plan.plan.openproject_snapshot
    snapshot = OpenProjectSnapshot(
        captured_at=reference.captured_at,
        etag=reference.etag,
        sha256=reference.sha256,
        work_packages=(),
    )
    envelope = PublicationEnvelope(
        "a" * 40,
        artifact.sha256,
        artifact.blob_sha1,
        event,
        snapshot.sha256,
        snapshot.etag,
        str(uuid.uuid4()),
        "e" * 64,
        artifact.plan.plan.publication_identity,
    )
    operations = plan_diff(
        with_approved_commit(artifact.plan, envelope.approved_commit),
        snapshot,
        trace_id=envelope.trace_id,
    )
    return artifact, snapshot, envelope, operations


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_postgres_journal_migrates_orders_replays_and_fences() -> None:
    assert DATABASE_URL is not None
    first = PostgresPublicationJournal(DATABASE_URL)
    first.setup()
    first.setup()
    assert first.ready()
    event = f"journal-test-{uuid.uuid4()}"
    envelope = _envelope(event)
    operations = (_operation("z-last"), _operation("a-first"))
    recorded, complete = first.begin(envelope, operations)
    assert recorded == operations and not complete
    second = PostgresPublicationJournal(DATABASE_URL)
    with pytest.raises(PublicationJournalMismatch, match="already in progress"):
        second.resume(envelope)
    first.intent(operations[0])
    first.outcome(operations[0])
    first.close()
    restored = PostgresPublicationJournal(DATABASE_URL)
    replay, complete = restored.resume(envelope) or ((), set())
    assert tuple(operation.operation_id for operation in replay) == ("z-last", "a-first")
    assert complete == {"z-last"}
    restored.close()


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_postgres_journal_serializes_concurrent_schema_setup() -> None:
    assert DATABASE_URL is not None
    workers = 8
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def setup() -> None:
        try:
            barrier.wait()
            journal = PostgresPublicationJournal(DATABASE_URL)
            journal.setup()
            assert journal.ready()
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=setup) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_postgres_journal_repairs_malformed_required_index() -> None:
    assert DATABASE_URL is not None
    with _temporary_database_url() as database_url:
        journal = PostgresPublicationJournal(database_url)
        journal.setup()
        assert journal.ready()
        with psycopg.connect(database_url) as connection, connection.transaction():
            connection.execute(
                "DROP INDEX planning_publication.publication_operations_ordinal"
            )
            connection.execute(
                """
                CREATE INDEX publication_operations_ordinal
                ON planning_publication.operations(approval_event_id, ordinal)
                """
            )

        assert not journal.ready()
        journal.setup()
        assert journal.ready()


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_postgres_journal_refuses_constraint_owned_malformed_index() -> None:
    assert DATABASE_URL is not None
    with _temporary_database_url() as database_url:
        journal = PostgresPublicationJournal(database_url)
        journal.setup()
        with psycopg.connect(database_url) as connection, connection.transaction():
            connection.execute(
                "DROP INDEX planning_publication.publication_operations_ordinal"
            )
            connection.execute(
                """
                ALTER TABLE planning_publication.operations
                ADD CONSTRAINT publication_operations_ordinal
                UNIQUE (approval_event_id, operation_id)
                """
            )

        assert not journal.ready()
        with pytest.raises(
            PublicationJournalMismatch,
            match="malformed required publication index is constraint-owned",
        ):
            journal.setup()


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_postgres_journal_refuses_malformed_required_constraint() -> None:
    assert DATABASE_URL is not None
    with _temporary_database_url() as database_url:
        journal = PostgresPublicationJournal(database_url)
        journal.setup()
        with psycopg.connect(database_url) as connection, connection.transaction():
            connection.execute(
                """
                ALTER TABLE planning_publication.publications
                DROP CONSTRAINT publications_target_sha256
                """
            )
            connection.execute(
                """
                ALTER TABLE planning_publication.publications
                ADD CONSTRAINT publications_target_sha256
                CHECK (publication_target_sha256 <> '')
                """
            )

        assert not journal.ready()
        with pytest.raises(
            PublicationJournalMismatch,
            match="required publication constraint does not match its contract",
        ):
            journal.setup()


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_postgres_journal_restores_missing_key_constraints() -> None:
    assert DATABASE_URL is not None
    with _temporary_database_url() as database_url:
        journal = PostgresPublicationJournal(database_url)
        journal.setup()
        with psycopg.connect(database_url) as connection, connection.transaction():
            connection.execute(
                """
                ALTER TABLE planning_publication.operations
                DROP CONSTRAINT operations_approval_event_id_fkey,
                DROP CONSTRAINT operations_pkey
                """
            )
            connection.execute(
                """
                ALTER TABLE planning_publication.publications
                DROP CONSTRAINT publications_pkey
                """
            )

        assert not journal.ready()
        journal.setup()
        assert journal.ready()


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_postgres_journal_backfills_legacy_ordinals_before_not_null() -> None:
    assert DATABASE_URL is not None
    with _temporary_database_url() as legacy_database_url:
        event = f"legacy-journal-{uuid.uuid4()}"
        with (
            psycopg.connect(legacy_database_url) as connection,
            connection.transaction(),
        ):
            connection.execute("CREATE SCHEMA planning_publication")
            connection.execute(
                """
                CREATE TABLE planning_publication.publications (
                  approval_event_id text PRIMARY KEY,
                  envelope_sha256 text NOT NULL,
                  operations_sha256 text NOT NULL,
                  state text NOT NULL DEFAULT 'in_progress',
                  created_at timestamptz NOT NULL DEFAULT now(),
                  updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE planning_publication.operations (
                  approval_event_id text NOT NULL
                    REFERENCES planning_publication.publications(approval_event_id),
                  operation_id text NOT NULL,
                  operation_sha256 text NOT NULL,
                  operation jsonb NOT NULL,
                  state text NOT NULL DEFAULT 'planned',
                  intent_at timestamptz,
                  outcome_at timestamptz,
                  error_code text,
                  result jsonb,
                  PRIMARY KEY (approval_event_id, operation_id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO planning_publication.publications(
                  approval_event_id, envelope_sha256, operations_sha256
                ) VALUES (%s, %s, %s)
                """,
                (event, "a" * 64, "b" * 64),
            )
            for operation_id in ("z-last", "a-first"):
                connection.execute(
                    """
                    INSERT INTO planning_publication.operations(
                      approval_event_id, operation_id, operation_sha256, operation
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (
                        event,
                        operation_id,
                        "c" * 64,
                        Jsonb({"operation_id": operation_id}),
                    ),
                )

        journal = PostgresPublicationJournal(legacy_database_url)
        with pytest.raises(
            PublicationJournalMismatch,
            match="unfinished legacy publication",
        ):
            journal.setup()
        with (
            psycopg.connect(legacy_database_url) as connection,
            connection.transaction(),
        ):
            connection.execute(
                """
                UPDATE planning_publication.publications
                SET state='completed'
                WHERE approval_event_id=%s
                """,
                (event,),
            )
            connection.execute(
                """
                UPDATE planning_publication.operations
                SET state='applied'
                WHERE approval_event_id=%s
                """,
                (event,),
            )
        journal.setup()
        assert journal.ready()
        with psycopg.connect(legacy_database_url) as connection:
            ordinals = connection.execute(
                """
                SELECT operation_id, ordinal
                FROM planning_publication.operations
                WHERE approval_event_id=%s
                ORDER BY ordinal
                """,
                (event,),
            ).fetchall()
            identity = connection.execute(
                """
                SELECT publication_identity, legacy_archive
                FROM planning_publication.publications
                WHERE approval_event_id=%s
                """,
                (event,),
            ).fetchone()
        assert ordinals == [("a-first", 0), ("z-last", 1)]
        assert identity == (f"legacy:{event}", True)
        with pytest.raises(
            PublicationJournalMismatch,
            match="archived and cannot be replayed",
        ):
            PostgresPublicationJournal(legacy_database_url).resume(_envelope(event))


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_v5_migration_refuses_unfinished_v4_rows_and_archives_terminal_rows() -> None:
    assert DATABASE_URL is not None
    with _temporary_database_url() as legacy_database_url:
        journal = PostgresPublicationJournal(legacy_database_url)
        journal.setup()
        event = f"v4-targetless-{uuid.uuid4()}"
        operation = _operation("legacy-operation")
        with (
            psycopg.connect(legacy_database_url) as connection,
            connection.transaction(),
        ):
            connection.execute(
                "DELETE FROM planning_publication.schema_migrations WHERE marker=%s",
                (PostgresPublicationJournal.MIGRATION_MARKER,),
            )
            connection.execute(
                "INSERT INTO planning_publication.schema_migrations(marker) VALUES ('publication-journal-v4')"
            )
            connection.execute(
                "ALTER TABLE planning_publication.publications DROP CONSTRAINT publications_target_sha256"
            )
            connection.execute(
                "ALTER TABLE planning_publication.publications DROP COLUMN publication_target_sha256"
            )
            connection.execute(
                """
                INSERT INTO planning_publication.publications(
                  approval_event_id, publication_identity, envelope_sha256,
                  operations_sha256, state
                ) VALUES (%s, %s, %s, %s, 'in_progress')
                """,
                (event, "v4-targetless:v1", "a" * 64, "b" * 64),
            )
            connection.execute(
                """
                INSERT INTO planning_publication.operations(
                  approval_event_id, operation_id, ordinal,
                  operation_sha256, operation, state
                ) VALUES (%s, %s, 0, %s, %s, 'planned')
                """,
                (event, operation.operation_id, "c" * 64, Jsonb(operation.__dict__)),
            )

        with pytest.raises(
            PublicationJournalMismatch,
            match="unfinished pre-target publication",
        ):
            journal.setup()

        with (
            psycopg.connect(legacy_database_url) as connection,
            connection.transaction(),
        ):
            connection.execute(
                "UPDATE planning_publication.publications SET state='completed' WHERE approval_event_id=%s",
                (event,),
            )
            connection.execute(
                "UPDATE planning_publication.operations SET state='applied' WHERE approval_event_id=%s",
                (event,),
            )
            connection.execute("CREATE SCHEMA planning_lifecycle")
            connection.execute(
                """
                CREATE TABLE planning_lifecycle.plan_runs (
                  plan_id text NOT NULL,
                  plan_version integer NOT NULL,
                  state text NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO planning_lifecycle.plan_runs(plan_id, plan_version, state)
                VALUES ('v4-targetless', 1, 'publishing')
                """
            )

        with pytest.raises(
            PublicationJournalMismatch,
            match="terminal pre-target publication requires lifecycle resolution",
        ):
            journal.setup()

        with (
            psycopg.connect(legacy_database_url) as connection,
            connection.transaction(),
        ):
            connection.execute(
                """
                UPDATE planning_lifecycle.plan_runs
                SET state='published'
                WHERE plan_id='v4-targetless' AND plan_version=1
                """
            )

        journal.setup()
        assert journal.ready()
        with psycopg.connect(legacy_database_url) as connection:
            migrated = connection.execute(
                """
                SELECT publication_identity, legacy_archive,
                       publication_target_sha256
                FROM planning_publication.publications
                WHERE approval_event_id=%s
                """,
                (event,),
            ).fetchone()
        assert migrated == (f"legacy:{event}", True, "0" * 64)
        with pytest.raises(
            PublicationJournalMismatch,
            match="archived and cannot be replayed",
        ):
            PostgresPublicationJournal(legacy_database_url).resume(_envelope(event))


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_postgres_journal_round_trips_tuple_identities_and_terminal_replay() -> None:
    assert DATABASE_URL is not None
    journal = PostgresPublicationJournal(DATABASE_URL)
    journal.setup()
    event = f"journal-json-{uuid.uuid4()}"
    envelope = _envelope(event)
    operation = PublicationOperation(
        "relation",
        "create_relation",
        ("plan", "child"),
        {"target_identity": ("plan", "parent")},
        None,
        None,
        "trace",
        {},
    )
    journal.begin(envelope, (operation,))
    journal.intent(operation)
    journal.outcome(operation)
    journal.finalize()
    restored = PostgresPublicationJournal(DATABASE_URL)
    replay, completed = restored.resume(envelope) or ((), set())
    assert replay == (operation,)
    assert completed == {"relation"}
    restored.close()


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_postgres_journal_rejects_invalid_terminal_transition() -> None:
    assert DATABASE_URL is not None
    journal = PostgresPublicationJournal(DATABASE_URL)
    journal.setup()
    event = f"journal-transition-{uuid.uuid4()}"
    operation = _operation("one")
    journal.begin(_envelope(event), (operation,))
    journal.intent(operation)
    journal.outcome(operation)
    with pytest.raises(PublicationJournalMismatch, match="transition"):
        journal.outcome(operation)
    journal.close()


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("mutation", ["operation_sha", "aggregate", "ordinal"])
def test_postgres_journal_rejects_tampered_recorded_operations(mutation: str) -> None:
    assert DATABASE_URL is not None
    journal = PostgresPublicationJournal(DATABASE_URL)
    journal.setup()
    event = f"journal-tamper-{uuid.uuid4()}"
    envelope = _envelope(event)
    operations = (_operation("first"), _operation("second"))
    journal.begin(envelope, operations)
    journal.close()
    with psycopg.connect(DATABASE_URL) as connection, connection.transaction():
        if mutation == "operation_sha":
            connection.execute(
                "UPDATE planning_publication.operations SET operation_sha256=%s WHERE approval_event_id=%s AND ordinal=0",
                ("0" * 64, event),
            )
        elif mutation == "aggregate":
            connection.execute(
                "UPDATE planning_publication.publications SET operations_sha256=%s WHERE approval_event_id=%s",
                ("0" * 64, event),
            )
        else:
            connection.execute(
                "UPDATE planning_publication.operations SET ordinal=7 WHERE approval_event_id=%s AND ordinal=1",
                (event,),
            )
    with pytest.raises(PublicationJournalMismatch):
        PostgresPublicationJournal(DATABASE_URL).resume(envelope)


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_postgres_journal_rejects_reused_approval_with_different_envelope() -> None:
    assert DATABASE_URL is not None
    journal = PostgresPublicationJournal(DATABASE_URL)
    journal.setup()
    event = f"journal-envelope-{uuid.uuid4()}"
    original = _envelope(event)
    journal.begin(original, (_operation("one"),))
    journal.close()
    changed = PublicationEnvelope(
        "b" * 40,
        "b" * 64,
        "c" * 40,
        event,
        "d" * 64,
        "etag",
        "trace",
        original.publication_target_sha256,
        original.publication_identity,
    )
    with pytest.raises(PublicationJournalMismatch):
        PostgresPublicationJournal(DATABASE_URL).resume(changed)


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize(
    "error,state",
    [(AmbiguousPublicationEffect("lost"), "ambiguous"), (RuntimeError("retry"), "retryable")],
)
def test_postgres_journal_persists_ambiguous_and_retryable_states(
    error: Exception, state: str
) -> None:
    assert DATABASE_URL is not None
    journal = PostgresPublicationJournal(DATABASE_URL)
    journal.setup()
    event = f"journal-state-{uuid.uuid4()}"
    operation = _operation("one")
    journal.begin(_envelope(event), (operation,))
    journal.intent(operation)
    journal.failure(operation, error)
    journal.close()
    with psycopg.connect(DATABASE_URL) as connection:
        row = connection.execute(
            "SELECT state FROM planning_publication.operations WHERE approval_event_id=%s", (event,)
        ).fetchone()
    assert row == (state,)


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_publisher_terminal_replay_has_no_adapter_effects() -> None:
    assert DATABASE_URL is not None
    artifact, snapshot, envelope, _ = _publication(f"journal-terminal-{uuid.uuid4()}")

    class ApplyingAdapter:
        publication_target_sha256 = TARGET_SHA256
        effects = 0

        def snapshot(self):
            return snapshot

        def resolve(self, identity):
            return None

        def postcondition(self, operation):
            return False

        def apply(self, operation, *, idempotency_key, current):
            self.effects += 1

    first = ApplyingAdapter()
    journal = PostgresPublicationJournal(DATABASE_URL)
    journal.setup()
    initial = publish(artifact, first, envelope, apply=True, journal=journal)
    assert initial.applied and first.effects == len(initial.operations)
    assert initial.applied_operations == initial.operations

    class NoEffectAdapter:
        def __getattribute__(self, name):
            if name == "publication_target_sha256":
                return TARGET_SHA256
            if name.startswith("_"):
                return super().__getattribute__(name)
            raise AssertionError(f"terminal replay called adapter.{name}")

    replayed = publish(
        artifact,
        NoEffectAdapter(),
        envelope,
        apply=True,
        journal=PostgresPublicationJournal(DATABASE_URL),
    )
    assert replayed.operations == initial.operations
    assert replayed.resumed is True
    assert replayed.applied_operations == ()


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_publisher_rejects_different_artifact_before_journal_resume() -> None:
    assert DATABASE_URL is not None
    artifact, _, envelope, operations = _publication(f"journal-artifact-{uuid.uuid4()}")
    journal = PostgresPublicationJournal(DATABASE_URL)
    journal.setup()
    journal.begin(envelope, operations)
    journal.close()
    different = load_artifact(
        Path(__file__).parents[1] / "evals/fixtures/bug-remediation/backlog.yaml"
    )

    class NoEffectAdapter:
        publication_target_sha256 = TARGET_SHA256

        def __getattribute__(self, name):
            if name.startswith("_"):
                return super().__getattribute__(name)
            raise AssertionError(f"artifact mismatch called adapter.{name}")

    with pytest.raises(PublicationRejected, match="SHA-256"):
        publish(
            different,
            NoEffectAdapter(),
            envelope,
            apply=True,
            journal=PostgresPublicationJournal(DATABASE_URL),
        )
    # The failed call never acquired the approval fence.
    resumed = PostgresPublicationJournal(DATABASE_URL)
    assert resumed.resume(envelope) is not None
    resumed.close()
    assert artifact.sha256 == envelope.backlog_sha256


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_publisher_exception_releases_postgres_fence() -> None:
    assert DATABASE_URL is not None
    artifact, _, envelope, _ = _publication(f"journal-exception-{uuid.uuid4()}")
    setup = PostgresPublicationJournal(DATABASE_URL)
    setup.setup()

    class BrokenSnapshotAdapter:
        publication_target_sha256 = TARGET_SHA256

        def snapshot(self):
            raise RuntimeError("injected snapshot failure")

        def resolve(self, identity):
            raise AssertionError

        def postcondition(self, operation):
            raise AssertionError

        def apply(self, operation, *, idempotency_key, current):
            raise AssertionError

    with pytest.raises(RuntimeError, match="injected"):
        publish(
            artifact,
            BrokenSnapshotAdapter(),
            envelope,
            apply=True,
            journal=PostgresPublicationJournal(DATABASE_URL),
        )
    reconstructed = PostgresPublicationJournal(DATABASE_URL)
    assert reconstructed.resume(envelope) is None
    reconstructed.close()


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_two_concurrent_publishers_allow_only_one_effecting_attempt() -> None:
    assert DATABASE_URL is not None
    artifact, snapshot, envelope, _ = _publication(f"journal-concurrent-{uuid.uuid4()}")
    setup = PostgresPublicationJournal(DATABASE_URL)
    setup.setup()
    entered = threading.Event()
    release = threading.Event()
    effects: list[str] = []
    first_error: list[BaseException] = []

    class BlockingAdapter:
        publication_target_sha256 = TARGET_SHA256

        def snapshot(self):
            return snapshot

        def resolve(self, identity):
            return None

        def postcondition(self, operation):
            return False

        def apply(self, operation, *, idempotency_key, current):
            effects.append(operation.operation_id)
            if len(effects) == 1:
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test release timeout")

    def run_first() -> None:
        try:
            publish(
                artifact,
                BlockingAdapter(),
                envelope,
                apply=True,
                journal=PostgresPublicationJournal(DATABASE_URL),
            )
        except BaseException as error:  # pragma: no cover - asserted below
            first_error.append(error)

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(timeout=5)
    with pytest.raises(PublicationRejected, match="already in progress"):
        publish(
            artifact,
            BlockingAdapter(),
            envelope,
            apply=True,
            journal=PostgresPublicationJournal(DATABASE_URL),
        )
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first_error == []
    # One complete operation sequence ran; the contender performed no effect.
    assert len(effects) == 2


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_plan_identity_fence_serializes_different_events_but_not_other_plans() -> None:
    assert DATABASE_URL is not None
    setup = PostgresPublicationJournal(DATABASE_URL)
    setup.setup()
    identity = f"plan-fence-{uuid.uuid4()}:v1"
    first_envelope = PublicationEnvelope(
        "a" * 40,
        "b" * 64,
        "c" * 40,
        f"event-a-{uuid.uuid4()}",
        "d" * 64,
        "etag",
        "trace",
        "e" * 64,
        identity,
    )
    same_plan_other_event = PublicationEnvelope(
        "a" * 40,
        "b" * 64,
        "c" * 40,
        f"event-b-{uuid.uuid4()}",
        "d" * 64,
        "etag",
        "trace",
        "e" * 64,
        identity.removesuffix(":v1") + ":v2",
    )
    other_plan = PublicationEnvelope(
        "a" * 40,
        "b" * 64,
        "c" * 40,
        f"event-c-{uuid.uuid4()}",
        "d" * 64,
        "etag",
        "trace",
        "e" * 64,
        f"plan-other-{uuid.uuid4()}:v1",
    )
    first = PostgresPublicationJournal(DATABASE_URL)
    first.begin(first_envelope, (_operation("one"),))
    with pytest.raises(PublicationJournalMismatch, match="already in progress"):
        PostgresPublicationJournal(DATABASE_URL).resume(same_plan_other_event)
    independent = PostgresPublicationJournal(DATABASE_URL)
    independent.begin(other_plan, (_operation("two"),))
    independent.close()
    first.close()


@pytest.mark.parametrize(
    "identity",
    [
        "",
        "plan",
        "plan:v0",
        "Plan:v1",
        "ab:v1",
        "plan_:v1",
        f"{'a' * 65}:v1",
        "plan:v1:extra",
    ],
)
def test_publication_scope_rejects_invalid_contract_identities(identity: str) -> None:
    with pytest.raises(PublicationJournalMismatch, match="identity is invalid"):
        publication_scope(identity)


def test_publication_scope_is_stable_across_plan_versions() -> None:
    assert publication_scope("plan-stable:v1") == "plan-stable"
    assert publication_scope("plan-stable:v2048") == "plan-stable"


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize(
    ("failed_state", "postcondition", "expected_applies", "rejected"),
    [
        ("planned", True, 2, False),
        ("ambiguous", False, 0, True),
        ("ambiguous", True, 1, False),
        ("retryable", False, 2, False),
        ("intent", False, 0, True),
    ],
)
def test_publisher_recovery_respects_recorded_effect_state(
    failed_state: str,
    postcondition: bool,
    expected_applies: int,
    rejected: bool,
) -> None:
    assert DATABASE_URL is not None
    artifact, _, envelope, operations = _publication(
        f"journal-recovery-{failed_state}-{uuid.uuid4()}"
    )
    journal = PostgresPublicationJournal(DATABASE_URL)
    journal.setup()
    journal.begin(envelope, operations)
    if failed_state != "planned":
        journal.intent(operations[0])
    if failed_state == "ambiguous":
        journal.failure(operations[0], AmbiguousPublicationEffect("lost"))
    elif failed_state == "retryable":
        journal.failure(operations[0], RuntimeError("definite failure"))
    journal.close()
    applies: list[str] = []

    class RecoveryAdapter:
        publication_target_sha256 = TARGET_SHA256

        def snapshot(self):
            raise AssertionError("journal replay must not take a new base snapshot")

        def resolve(self, identity):
            return None

        def postcondition(self, operation):
            if failed_state in {"planned", "retryable"}:
                raise AssertionError("planned/retryable states must not recover by postcondition")
            return postcondition and operation.operation_id == operations[0].operation_id

        def apply(self, operation, *, idempotency_key, current):
            applies.append(operation.operation_id)

    def call():
        return publish(
            artifact,
            RecoveryAdapter(),
            envelope,
            apply=True,
            journal=PostgresPublicationJournal(DATABASE_URL),
        )

    if rejected:
        with pytest.raises(PublicationRejected, match="ambiguous"):
            call()
    else:
        result = call()
        assert result.resumed is True
        assert len(result.applied_operations) == expected_applies
    assert len(applies) == expected_applies


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_retryable_resume_rechecks_current_state_before_new_intent() -> None:
    assert DATABASE_URL is not None
    artifact, _, envelope, operations = _publication(f"journal-stale-retry-{uuid.uuid4()}")
    journal = PostgresPublicationJournal(DATABASE_URL)
    journal.setup()
    journal.begin(envelope, operations)
    journal.intent(operations[0])
    journal.failure(operations[0], RuntimeError("definite failure"))
    journal.close()

    class StaleAdapter:
        publication_target_sha256 = TARGET_SHA256

        def snapshot(self):
            raise AssertionError("journal replay must not snapshot")

        def resolve(self, identity):
            from planning_platform.openproject import WorkPackageSnapshot

            return WorkPackageSnapshot(1, 1, identity[0], identity[1], managed_hash="stale")

        def postcondition(self, operation):
            raise AssertionError("retryable state must not recover by postcondition")

        def apply(self, operation, *, idempotency_key, current):
            raise AssertionError("stale retryable operation must not apply")

    with pytest.raises(PublicationRejected, match="identity already exists"):
        publish(
            artifact,
            StaleAdapter(),
            envelope,
            apply=True,
            journal=PostgresPublicationJournal(DATABASE_URL),
        )
