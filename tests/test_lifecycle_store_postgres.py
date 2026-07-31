from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from planning_platform.lifecycle.dedupe import PostgresDeliveryDeduplicator
from planning_platform.lifecycle.models import (
    EventActor,
    EventSubject,
    VerifiedSignature,
    envelope_for_delivery,
)
from planning_platform.lifecycle.store import (
    LifecycleStoreMismatch,
    PlanRun,
    PostgresLifecycleStore,
)

DATABASE_URL = os.environ.get("PLANNER_TEST_DATABASE_URL")
pytestmark = pytest.mark.postgres


def _run(unique: str) -> PlanRun:
    numeric = int(unique[:12], 16)
    return PlanRun(
        idea_id=numeric,
        plan_id=f"idea-{numeric}",
        plan_version=1,
        thread_id=f"openproject:{numeric}:planning:1",
        repository="RausserHQ/planning-platform",
        base_branch="a" * 40,
        artifact_prefix=f"planning/idea-{numeric}/v1",
        backlog_path=f"planning/idea-{numeric}/v1/backlog.yaml",
        snapshot_sha256="b" * 64,
        snapshot_etag=unique,
        state="planning",
    )


def _event(unique: str):
    now = datetime.now(UTC)
    return envelope_for_delivery(
        event_type="reconciliation.scheduled",
        source="scheduler",
        delivery_id=f"lifecycle-test:{unique}",
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="system", id="test"),
        subject=EventSubject(),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={"test": unique},
    )


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_lifecycle_store_replays_immutable_binding_and_tracks_pr_state() -> None:
    assert DATABASE_URL is not None
    store = PostgresLifecycleStore(DATABASE_URL)
    store.setup()
    store.setup()
    assert store.ready()
    run = _run(uuid4().hex)
    assert store.begin(run) == run
    assert store.begin(run) == run
    with pytest.raises(LifecycleStoreMismatch, match="immutable"):
        store.begin(
            PlanRun(
                **{
                    **run.__dict__,
                    "base_branch": "c" * 40,
                }
            )
        )
    pull_number = run.idea_id % 2_000_000_000 + 1
    recorded = store.record_pull_request(
        plan_id=run.plan_id,
        plan_version=run.plan_version,
        planning_commit="d" * 40,
        number=pull_number,
        url=f"https://github.com/RausserHQ/planning-platform/pull/{pull_number}",
    )
    assert recorded.state == "pr_open"
    assert store.by_pull_request(run.repository, pull_number) == recorded
    publishing = store.set_state(recorded, "publishing")
    published = store.set_state(publishing, "published")
    assert store.latest_for_idea(run.idea_id) == published
    with pytest.raises(LifecycleStoreMismatch, match="transition"):
        store.set_state(published, "publishing")


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_lifecycle_store_and_delivery_claim_are_concurrency_safe() -> None:
    assert DATABASE_URL is not None
    store = PostgresLifecycleStore(DATABASE_URL)
    store.setup()
    run = _run(uuid4().hex)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _: store.begin(run), range(8)))
    assert results == (run,) * 8

    deduplicator = PostgresDeliveryDeduplicator(
        DATABASE_URL,
        lease=timedelta(seconds=2),
    )
    deduplicator.setup()
    deduplicator.setup()
    assert deduplicator.ready()
    event = _event(uuid4().hex)
    now = datetime.now(UTC)
    first = deduplicator.claim(event, now=now)
    assert first.acquired and first.claim_token is not None
    assert not deduplicator.claim(event, now=now).acquired
    deduplicator.retry(
        event,
        claim_token=first.claim_token,
        reason="retryable-test",
        now=now,
    )
    second = deduplicator.claim(event, now=now)
    assert second.acquired and second.claim_token is not None
    with pytest.raises(ValueError, match="claimed"):
        deduplicator.complete(
            event,
            claim_token=first.claim_token,
            now=datetime.now(UTC),
        )
    deduplicator.complete(
        event,
        claim_token=second.claim_token,
        now=datetime.now(UTC),
    )
    assert deduplicator.claim(event, now=datetime.now(UTC)).state == "completed"

    dead_event = _event(uuid4().hex)
    dead_claim = deduplicator.claim(dead_event, now=now)
    assert dead_claim.claim_token is not None
    deduplicator.dead_letter(
        dead_event,
        claim_token=dead_claim.claim_token,
        reason="terminal-test",
        now=now,
    )
    recovered = deduplicator.recover_dead_letter(
        dead_event,
        operator="test-operator",
        reason="verified recovery",
        now=now,
    )
    assert recovered.acquired and recovered.claim_token is not None
    deduplicator.restore_dead_letter(
        dead_event,
        claim_token=recovered.claim_token,
        reason="recovery effect failed",
        now=datetime.now(UTC),
    )
    recovered = deduplicator.recover_dead_letter(
        dead_event,
        operator="test-operator",
        reason="verified second recovery",
        now=datetime.now(UTC),
    )
    assert recovered.acquired and recovered.claim_token is not None
    deduplicator.complete(
        dead_event,
        claim_token=recovered.claim_token,
        now=datetime.now(UTC),
    )


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_expired_delivery_owner_cannot_finalize_a_new_owner_claim() -> None:
    assert DATABASE_URL is not None
    deduplicator = PostgresDeliveryDeduplicator(
        DATABASE_URL,
        lease=timedelta(milliseconds=20),
    )
    deduplicator.setup()
    event = _event(uuid4().hex)
    first = deduplicator.claim(event, now=datetime.now(UTC))
    assert first.claim_token is not None
    time.sleep(0.04)
    # Releasing the session fence models the original worker process dying.
    deduplicator.release_fence(event, claim_token=first.claim_token)
    second = deduplicator.claim(event, now=datetime.now(UTC))
    assert second.acquired and second.claim_token is not None
    with pytest.raises(ValueError, match="claimed"):
        deduplicator.complete(
            event,
            claim_token=first.claim_token,
            now=datetime.now(UTC),
        )
    deduplicator.complete(
        event,
        claim_token=second.claim_token,
        now=datetime.now(UTC),
    )


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_runtime_readiness_rejects_legacy_columns_constraints_and_indexes() -> None:
    assert DATABASE_URL is not None
    store = PostgresLifecycleStore(DATABASE_URL)
    deduplicator = PostgresDeliveryDeduplicator(DATABASE_URL)

    try:
        with psycopg.connect(DATABASE_URL) as connection, connection.transaction():
            connection.execute("DROP SCHEMA IF EXISTS planning_lifecycle CASCADE")
            connection.execute("CREATE SCHEMA planning_lifecycle")
            connection.execute("CREATE TABLE planning_lifecycle.plan_runs (plan_id text)")
            connection.execute("CREATE TABLE planning_lifecycle.audit (audit_id bigint)")
            connection.execute(
                """
                CREATE TABLE planning_lifecycle.delivery_deduplications (
                    idempotency_key text PRIMARY KEY
                )
                """
            )
        assert not store.ready()
        assert not deduplicator.ready()

        with psycopg.connect(DATABASE_URL) as connection, connection.transaction():
            connection.execute("DROP SCHEMA planning_lifecycle CASCADE")
        store.setup()
        deduplicator.setup()
        assert store.ready()
        assert deduplicator.ready()

        with psycopg.connect(DATABASE_URL) as connection, connection.transaction():
            connection.execute(
                """
                ALTER TABLE planning_lifecycle.plan_runs
                DROP COLUMN start_request_ciphertext
                """
            )
        assert not store.ready()
        store.setup()
        assert store.ready()

        with psycopg.connect(DATABASE_URL) as connection, connection.transaction():
            connection.execute("DROP INDEX planning_lifecycle.plan_runs_repository_pr")
            connection.execute(
                """
                CREATE UNIQUE INDEX plan_runs_repository_pr
                ON planning_lifecycle.plan_runs(repository, pull_request_number)
                WHERE pull_request_number IS NOT NULL AND false
                """
            )
        assert not store.ready()
        with psycopg.connect(DATABASE_URL) as connection, connection.transaction():
            connection.execute("DROP INDEX planning_lifecycle.plan_runs_repository_pr")
        store.setup()
        assert store.ready()

        with psycopg.connect(DATABASE_URL) as connection, connection.transaction():
            connection.execute(
                """
                ALTER TABLE planning_lifecycle.delivery_deduplications
                DROP COLUMN claim_token
                """
            )
        assert not deduplicator.ready()
        deduplicator.setup()
        assert deduplicator.ready()

        with psycopg.connect(DATABASE_URL) as connection, connection.transaction():
            constraint_name = connection.execute(
                """
                SELECT constraint_record.conname
                FROM pg_constraint AS constraint_record
                JOIN pg_class AS relation
                  ON relation.oid = constraint_record.conrelid
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'planning_lifecycle'
                  AND relation.relname = 'delivery_deduplications'
                  AND constraint_record.contype = 'c'
                """
            ).fetchone()
            assert constraint_name is not None
            connection.execute(
                psycopg.sql.SQL(
                    """
                    ALTER TABLE planning_lifecycle.delivery_deduplications
                    DROP CONSTRAINT {}
                    """
                ).format(psycopg.sql.Identifier(str(constraint_name[0])))
            )
            connection.execute(
                """
                ALTER TABLE planning_lifecycle.delivery_deduplications
                ADD CONSTRAINT legacy_delivery_state
                CHECK (state IN ('claimed', 'completed'))
                """
            )
        assert not deduplicator.ready()
    finally:
        with psycopg.connect(DATABASE_URL) as connection, connection.transaction():
            connection.execute("DROP SCHEMA IF EXISTS planning_lifecycle CASCADE")
        store.setup()
        deduplicator.setup()
