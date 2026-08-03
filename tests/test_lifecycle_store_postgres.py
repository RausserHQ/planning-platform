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
    publishing = store.approve_for_publication(
        recorded,
        merge_commit="e" * 40,
        evidence_sha256="f" * 64,
    )
    assert publishing.approved_commit == "e" * 40
    assert publishing.approval_evidence_sha256 == "f" * 64
    assert (
        store.approve_for_publication(
            publishing,
            merge_commit="e" * 40,
            evidence_sha256="f" * 64,
        )
        == publishing
    )
    with pytest.raises(LifecycleStoreMismatch, match="approval evidence"):
        store.approve_for_publication(
            publishing,
            merge_commit="e" * 40,
            evidence_sha256="0" * 64,
        )
    bound = store.record_publication_binding(
        publishing,
        approved_commit="e" * 40,
        backlog_blob_sha1="1" * 40,
        backlog_sha256="2" * 64,
    )
    assert bound.backlog_blob_sha1 == "1" * 40
    assert bound.backlog_sha256 == "2" * 64
    assert (
        store.record_publication_binding(
            bound,
            approved_commit="e" * 40,
            backlog_blob_sha1="1" * 40,
            backlog_sha256="2" * 64,
        )
        == bound
    )
    published = store.set_state(bound, "published")
    assert store.latest_for_idea(run.idea_id) == published
    assert store.latest_published(run.plan_id) == published
    with pytest.raises(LifecycleStoreMismatch, match="transition"):
        store.set_state(published, "publishing")


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_stale_thread_count_uses_latest_nonterminal_run_and_includes_cutoff_boundary() -> None:
    assert DATABASE_URL is not None
    store = PostgresLifecycleStore(DATABASE_URL)
    store.setup()
    cutoff = datetime.now(UTC).replace(microsecond=0)
    older = cutoff - timedelta(seconds=1)
    newer = cutoff + timedelta(seconds=1)
    latest_terminal = _run(uuid4().hex)
    older_version = PlanRun(
        **{
            **latest_terminal.__dict__,
            "plan_version": 2,
            "thread_id": f"{latest_terminal.thread_id}:v2",
            "snapshot_etag": f"{latest_terminal.snapshot_etag}:v2",
        }
    )
    stale = _run(uuid4().hex)
    boundary = _run(uuid4().hex)
    failed = _run(uuid4().hex)
    fresh = _run(uuid4().hex)
    for run in (latest_terminal, older_version, stale, boundary, failed, fresh):
        store.begin(run)
    with psycopg.connect(DATABASE_URL) as connection, connection.transaction():
        for run, state, updated_at in (
            (latest_terminal, "planning", older),
            (older_version, "published", older),
            (stale, "planning", older),
            (boundary, "needs_input", cutoff),
            (failed, "failed", older),
            (fresh, "planning", newer),
        ):
            connection.execute(
                """
                UPDATE planning_lifecycle.plan_runs
                SET state=%s, updated_at=%s
                WHERE plan_id=%s AND plan_version=%s
                """,
                (state, updated_at, run.plan_id, run.plan_version),
            )

    assert store.stale_thread_count(cutoff) == 2


@pytest.mark.skipif(not DATABASE_URL, reason="PLANNER_TEST_DATABASE_URL is not set")
def test_implementation_pr_mapping_is_immutable_monotonic_and_head_bound() -> None:
    assert DATABASE_URL is not None
    store = PostgresLifecycleStore(DATABASE_URL)
    store.setup()
    number = int(uuid4().hex[:7], 16) + 1
    repository = "RausserHQ/planning-platform"
    opened_at = datetime.now(UTC)
    values = {
        "repository": repository,
        "number": number,
        "plan_id": f"idea-{number}",
        "node_key": "implementation-core",
        "work_package_id": number,
        "url": f"https://github.com/{repository}/pull/{number}",
        "head_sha": "1" * 40,
        "observed_at": opened_at,
        "pull_request_state": "open",
        "merged_commit": None,
    }

    with ThreadPoolExecutor(max_workers=8) as executor:
        bindings = tuple(
            executor.map(
                lambda _index: store.bind_implementation_pull_request(**values),
                range(8),
            )
        )
    assert bindings == (bindings[0],) * 8
    assert store.by_implementation_pull_request(repository, number) == bindings[0]

    with pytest.raises(LifecycleStoreMismatch, match="identity"):
        store.bind_implementation_pull_request(
            **{**values, "node_key": "retargeted-node"}
        )
    assert (
        store.record_implementation_check_result(
            repository,
            number,
            head_sha="2" * 40,
            passed=True,
        )
        is None
    )
    checked = store.record_implementation_check_result(
        repository,
        number,
        head_sha="1" * 40,
        passed=True,
    )
    assert checked is not None and checked.successful_check_sha == "1" * 40
    cleared = store.record_implementation_check_result(
        repository,
        number,
        head_sha="1" * 40,
        passed=False,
    )
    assert cleared is not None and cleared.successful_check_sha is None
    checked = store.record_implementation_check_result(
        repository,
        number,
        head_sha="1" * 40,
        passed=True,
    )
    assert checked is not None and checked.successful_check_sha == "1" * 40

    pushed = store.bind_implementation_pull_request(
        **{
            **values,
            "head_sha": "2" * 40,
            "observed_at": opened_at + timedelta(seconds=2),
        }
    )
    assert pushed.head_sha == "2" * 40
    assert pushed.successful_check_sha is None
    late = store.bind_implementation_pull_request(
        **{
            **values,
            "observed_at": opened_at + timedelta(seconds=1),
        }
    )
    assert late.head_sha == "2" * 40

    merged = store.bind_implementation_pull_request(
        **{
            **values,
            "head_sha": "2" * 40,
            "observed_at": opened_at + timedelta(seconds=3),
            "pull_request_state": "closed",
            "merged_commit": "3" * 40,
        }
    )
    assert merged.merged_commit == "3" * 40
    replayed_open = store.bind_implementation_pull_request(
        **{
            **values,
            "head_sha": "2" * 40,
            "observed_at": opened_at + timedelta(seconds=4),
        }
    )
    assert replayed_open.merged_commit == "3" * 40
    assert replayed_open.pull_request_state == "closed"
    assert replayed_open in store.implementation_associations()


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
def test_terminal_resume_abandonment_atomically_clears_and_audits_exact_delivery() -> None:
    assert DATABASE_URL is not None
    store = PostgresLifecycleStore(DATABASE_URL)
    store.setup()
    deduplicator = PostgresDeliveryDeduplicator(DATABASE_URL)
    deduplicator.setup()
    run = store.begin(_run(uuid4().hex))
    run = store.set_state(run, "needs_input")
    ciphertext = f"v1:{uuid4().hex}"
    run = store.record_pending_resume(run, ciphertext)
    event = _event(uuid4().hex)
    claim = deduplicator.claim(event, now=datetime.now(UTC))
    assert claim.claim_token is not None
    deduplicator.dead_letter(
        event,
        claim_token=claim.claim_token,
        reason="terminal test delivery",
        now=datetime.now(UTC),
    )

    corrected = store.complete_terminal_resume_abandonment(
        run=run,
        request_ciphertext=ciphertext,
        idempotency_key=event.idempotency_key,
        trace_id=str(event.trace_id),
        interrupt_id="interrupt-1",
        operator="pilot-operator",
        reason="terminal delivery cannot be replayed",
    )
    replayed = store.complete_terminal_resume_abandonment(
        run=run,
        request_ciphertext=ciphertext,
        idempotency_key=event.idempotency_key,
        trace_id=str(event.trace_id),
        interrupt_id="interrupt-1",
        operator="pilot-operator",
        reason="terminal delivery cannot be replayed",
    )

    assert corrected.pending_resume_ciphertext is None
    assert replayed == corrected
    completed = store.completed_terminal_resume_abandonment(
        plan_id=run.plan_id,
        plan_version=run.plan_version,
        thread_id=run.thread_id,
        idempotency_key=event.idempotency_key,
        operator="pilot-operator",
        reason="terminal delivery cannot be replayed",
    )
    assert completed is not None and completed.interrupt_id == "interrupt-1"
    assert (
        store.completed_terminal_resume_abandonment(
            plan_id=run.plan_id,
            plan_version=run.plan_version,
            thread_id=run.thread_id,
            idempotency_key=event.idempotency_key,
            operator="different-operator",
            reason="different reason",
        )
        is None
    )
    assert store.terminal_delivery(event.idempotency_key) is not None
    with psycopg.connect(DATABASE_URL) as connection:
        audit_count = connection.execute(
            """
            SELECT count(*) FROM planning_lifecycle.audit
            WHERE event_id=%s AND action='terminal_resume_abandonment'
              AND outcome='restored_interrupt'
            """,
            (event.event_id,),
        ).fetchone()
    assert audit_count == (1,)


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
            connection.execute(
                """
                ALTER TABLE planning_lifecycle.implementation_pr_associations
                DROP CONSTRAINT implementation_pr_state_valid
                """
            )
            connection.execute(
                """
                ALTER TABLE planning_lifecycle.implementation_pr_associations
                ADD CONSTRAINT implementation_pr_state_valid
                CHECK (pull_request_state IN ('open','closed','invalid'))
                """
            )
        assert not store.ready()
        with psycopg.connect(DATABASE_URL) as connection, connection.transaction():
            connection.execute(
                """
                ALTER TABLE planning_lifecycle.implementation_pr_associations
                DROP CONSTRAINT implementation_pr_state_valid
                """
            )
        store.setup()
        assert store.ready()

        with psycopg.connect(DATABASE_URL) as connection, connection.transaction():
            connection.execute(
                """
                ALTER TABLE planning_lifecycle.plan_runs
                DROP CONSTRAINT plan_runs_approval_evidence_sha256_format
                """
            )
        assert not store.ready()
        store.setup()
        assert store.ready()

        with psycopg.connect(DATABASE_URL) as connection, connection.transaction():
            connection.execute(
                """
                ALTER TABLE planning_lifecycle.implementation_pr_associations
                DROP CONSTRAINT implementation_pr_state_valid
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
