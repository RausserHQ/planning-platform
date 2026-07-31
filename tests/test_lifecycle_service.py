from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import yaml

from planning_platform.github_models import ImmutableArtifactBinding
from planning_platform.lifecycle.models import (
    EventActor,
    EventSubject,
    VerifiedSignature,
    envelope_for_delivery,
)
from planning_platform.lifecycle.planner_client import PlannerThreadNotFound
from planning_platform.lifecycle.recovery import RecoveryCipher, RecoveryPayloadRejected
from planning_platform.lifecycle.service import LifecycleService
from planning_platform.lifecycle.store import PlanRun
from planning_platform.loader import load_artifact
from planning_platform.openproject import OpenProjectSnapshot, WorkPackageSnapshot
from planning_platform.planner.models import (
    PlanResponse,
    RepositoryFile,
    RepositorySnapshot,
    ResumePlanRequest,
    repository_snapshot_digest,
)
from planning_platform.publication_journal import InMemoryPublicationJournal


class MemoryStore:
    def __init__(self, run: PlanRun | None = None, *, crash_on_publish: bool = False) -> None:
        self.run = run
        self.audit_rows: list[tuple[str, str]] = []
        self.crash_on_publish = crash_on_publish

    def latest_for_idea(self, _idea_id: int) -> PlanRun | None:
        return self.run

    def begin(self, run: PlanRun) -> PlanRun:
        if self.run is None:
            self.run = run
        return self.run

    def set_state(self, run: PlanRun, state: str) -> PlanRun:
        if state == "published" and self.crash_on_publish:
            self.crash_on_publish = False
            raise RuntimeError("simulated crash after publication apply")
        self.run = replace(run, state=state)
        return self.run

    def by_pull_request(self, repository: str, number: int) -> PlanRun | None:
        if (
            self.run is not None
            and self.run.repository == repository
            and self.run.pull_request_number == number
        ):
            return self.run
        return None

    def record_publication_binding(
        self,
        run: PlanRun,
        *,
        approved_commit: str,
        backlog_blob_sha1: str,
        backlog_sha256: str,
    ) -> PlanRun:
        self.run = replace(
            run,
            approved_commit=approved_commit,
            backlog_blob_sha1=backlog_blob_sha1,
            backlog_sha256=backlog_sha256,
        )
        return self.run

    def clear_pending_resume(self, run: PlanRun, _request_ciphertext: str) -> PlanRun:
        assert self.run is not None
        self.run = replace(self.run, pending_resume_ciphertext=None)
        return self.run

    def audit(self, **values: Any) -> None:
        self.audit_rows.append((str(values["action"]), str(values["outcome"])))


class OpenProjectFake:
    def __init__(self) -> None:
        self.states: list[tuple[int, str | None, str | None]] = []
        self.comments: list[str] = []

    def read_work_package(self, work_package_id: int) -> dict[str, Any]:
        return {
            "id": work_package_id,
            "lockVersion": 3,
            "updatedAt": "2026-07-30T19:00:00Z",
            "subject": "Recover crash windows",
            "description": {"raw": "## Relevant repositories\nRausserHQ/planning-platform"},
        }

    def snapshot(self) -> OpenProjectSnapshot:
        return OpenProjectSnapshot(
            captured_at="2026-07-30T19:00:00Z",
            etag='"snapshot-1"',
            sha256="b" * 64,
            work_packages=(),
        )

    def ensure_comment(self, _work_package_id: int, body: str, *, idempotency_key: str) -> None:
        assert idempotency_key
        self.comments.append(body)

    def set_lifecycle_state(
        self,
        work_package_id: int,
        *,
        status: str | None = None,
        evidence_state: str | None = None,
    ) -> None:
        self.states.append((work_package_id, status, evidence_state))


class GitHubContextFake:
    async def context_snapshot(self, repository: str) -> tuple[str, RepositorySnapshot]:
        content = "# Planning Platform"
        file = RepositoryFile(
            path="README.md",
            sha256=hashlib.sha256(content.encode()).hexdigest(),
            content=content,
        )
        commit = "a" * 40
        return (
            commit,
            RepositorySnapshot(
                name=repository,
                commit=commit,
                snapshot_sha256=repository_snapshot_digest(
                    repository,
                    commit,
                    (file,),
                ),
                files=(file,),
            ),
        )


class CrashThenFailPlanner:
    def __init__(self) -> None:
        self.starts: list[object] = []

    async def get(self, _thread_id: str) -> PlanResponse:
        raise PlannerThreadNotFound("missing")

    async def start(self, request: object) -> PlanResponse:
        self.starts.append(request)
        if len(self.starts) == 1:
            raise RuntimeError("simulated lost request window")
        thread_id = str(getattr(request, "thread_id", ""))
        if not thread_id:
            thread_id = (
                f"openproject:{request.idea.work_package_id}:"  # type: ignore[attr-defined]
                f"planning:{request.plan_version}"  # type: ignore[attr-defined]
            )
        return PlanResponse(
            thread_id=thread_id,
            status="failed",
            trace_id=str(uuid4()),
            interrupt=None,
            artifact_manifest=(),
        )


def _cipher() -> RecoveryCipher:
    return RecoveryCipher(b"x" * 32)


def test_recovery_payload_is_authenticated_bound_and_not_plaintext() -> None:
    cipher = _cipher()
    plaintext = "human answer and repository context"
    sealed = cipher.seal(
        purpose="planner-start",
        binding="openproject:42:planning:1",
        plaintext=plaintext,
    )
    assert plaintext not in sealed
    assert (
        cipher.open(
            purpose="planner-start",
            binding="openproject:42:planning:1",
            ciphertext=sealed,
        )
        == plaintext
    )
    with pytest.raises(RecoveryPayloadRejected, match="unauthentic"):
        cipher.open(
            purpose="planner-start",
            binding="openproject:42:planning:2",
            ciphertext=sealed,
        )


def _work_package_event():
    now = datetime(2026, 7, 30, 19, 1, tzinfo=UTC)
    return envelope_for_delivery(
        event_type="openproject.work_package_changed",
        source="windmill",
        delivery_id="crash-start",
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="service", id="openproject"),
        subject=EventSubject(idea_id=42),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={
            "work_package": {
                "id": 42,
                "_links": {
                    "type": {"title": "Idea"},
                    "status": {"title": "Planning"},
                },
            }
        },
    )


@pytest.mark.asyncio
async def test_crash_after_run_insert_replays_the_exact_persisted_start_request() -> None:
    store = MemoryStore()
    planner = CrashThenFailPlanner()
    openproject = OpenProjectFake()
    service = LifecycleService(
        planner=planner,  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=GitHubContextFake(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
    )
    event = _work_package_event()
    with pytest.raises(RuntimeError, match="simulated"):
        await service.handle(event)
    assert store.run is not None
    assert store.run.start_request_ciphertext.startswith("v1:")
    assert "Recover crash windows" not in store.run.start_request_ciphertext
    assert "# Planning Platform" not in store.run.start_request_ciphertext

    outcome = await service.handle(event)
    assert outcome.outcome == "failed"
    assert len(planner.starts) == 2
    assert planner.starts[0] == planner.starts[1]
    assert store.run is not None and store.run.state == "failed"


class ResumeCrashPlanner:
    def __init__(self) -> None:
        self.requests: list[ResumePlanRequest] = []

    async def resume(self, thread_id: str, request: ResumePlanRequest) -> PlanResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            raise RuntimeError("simulated lost resume response")
        return PlanResponse(
            thread_id=thread_id,
            status="failed",
            trace_id=str(uuid4()),
            interrupt=None,
            artifact_manifest=(),
        )


@pytest.mark.asyncio
async def test_crash_after_resume_replays_the_persisted_idempotent_resume() -> None:
    request = ResumePlanRequest.model_validate(
        {
            "event": {
                "idempotency_key": "event:openproject:resume:12345678",
                "trace_id": str(uuid4()),
            },
            "interrupt_id": "interrupt-1",
            "comment_id": 19,
            "comment_created_at": "2026-07-30T19:00:00Z",
            "answer": "Use the bounded migration.",
        }
    )
    run = PlanRun(
        idea_id=42,
        plan_id="idea-42",
        plan_version=1,
        thread_id="openproject:42:planning:1",
        repository="RausserHQ/planning-platform",
        base_branch="a" * 40,
        artifact_prefix="planning/idea-42/v1",
        backlog_path="planning/idea-42/v1/backlog.yaml",
        snapshot_sha256="b" * 64,
        snapshot_etag='"snapshot-1"',
        state="needs_input",
        pending_resume_ciphertext=_cipher().seal(
            purpose="planner-resume",
            binding="openproject:42:planning:1",
            plaintext=request.model_dump_json(),
        ),
    )
    store = MemoryStore(run)
    planner = ResumeCrashPlanner()
    service = LifecycleService(
        planner=planner,  # type: ignore[arg-type]
        openproject=OpenProjectFake(),  # type: ignore[arg-type]
        github=object(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
    )
    now = datetime(2026, 7, 30, 19, 1, tzinfo=UTC)
    event = envelope_for_delivery(
        event_type="openproject.idea_comment",
        source="windmill",
        delivery_id="crash-resume",
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="human", id="7"),
        subject=EventSubject(idea_id=42),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={
            "work_package_comment": {
                "id": 19,
                "createdAt": "2026-07-30T19:00:00Z",
                "comment": {"raw": "Use the bounded migration."},
                "_links": {"workPackage": {"href": "/api/v3/work_packages/42"}},
            }
        },
    )
    with pytest.raises(RuntimeError, match="simulated"):
        await service.handle(event)
    outcome = await service.handle(event)
    assert outcome.outcome == "failed"
    assert planner.requests == [request, request]
    assert store.run is not None
    assert store.run.state == "failed"
    assert store.run.pending_resume_ciphertext is None


class PublicationOpenProjectFake(OpenProjectFake):
    def __init__(self, snapshot: OpenProjectSnapshot) -> None:
        super().__init__()
        self.publication_snapshot = snapshot
        self.effects: list[str] = []
        self.reject_snapshot_reads = False

    def snapshot(self) -> OpenProjectSnapshot:
        if self.reject_snapshot_reads:
            raise AssertionError("terminal publication replay performed a stale-base read")
        return self.publication_snapshot

    def resolve(self, _identity: tuple[str, str]):
        return None

    def apply(
        self,
        operation: object,
        *,
        idempotency_key: str,
        current: object,
    ) -> None:
        del current
        assert idempotency_key
        self.effects.append(str(operation.kind))

    def postcondition(self, _operation: object) -> bool:
        return False


class PublicationGitHubFake:
    def __init__(self, raw: bytes, binding: ImmutableArtifactBinding) -> None:
        self.raw = raw
        self.binding = binding

    async def artifact_binding(
        self,
        *,
        repository: str,
        commit_sha: str,
        path: str,
    ) -> ImmutableArtifactBinding:
        assert (repository, commit_sha, path) == (
            self.binding.repository,
            self.binding.commit_sha,
            self.binding.path,
        )
        return self.binding

    async def read_immutable_artifact(self, binding: ImmutableArtifactBinding) -> bytes:
        assert binding == self.binding
        return self.raw


@pytest.mark.asyncio
async def test_crash_after_publication_apply_replays_journal_before_stale_base_check() -> None:
    fixture = Path(__file__).parents[1] / "evals/fixtures/single-repository/backlog.yaml"
    raw = fixture.read_bytes()
    artifact = load_artifact(fixture)
    merge_sha = "a" * 40
    run = PlanRun(
        idea_id=1,
        plan_id="single-repository",
        plan_version=1,
        thread_id="openproject:1:planning:1",
        repository="Acme/service",
        base_branch=merge_sha,
        artifact_prefix="planning/single-repository/v1",
        backlog_path="planning/single-repository/v1/backlog.yaml",
        snapshot_sha256="b" * 64,
        snapshot_etag="fixture",
        state="pr_open",
        planning_commit="c" * 40,
        pull_request_number=17,
        pull_request_url="https://github.com/Acme/service/pull/17",
    )
    store = MemoryStore(run, crash_on_publish=True)
    snapshot = OpenProjectSnapshot(
        captured_at="2026-07-30T00:00:00Z",
        etag="fixture",
        sha256="b" * 64,
        work_packages=(),
    )
    openproject = PublicationOpenProjectFake(snapshot)
    binding = ImmutableArtifactBinding(
        repository=run.repository,
        commit_sha=merge_sha,
        path=run.backlog_path,
        blob_sha=artifact.blob_sha1,
        content_sha256=artifact.sha256,
    )
    journal = InMemoryPublicationJournal()
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=PublicationGitHubFake(raw, binding),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        publication_journal_factory=lambda: journal,
    )
    now = datetime(2026, 7, 30, 20, 0, tzinfo=UTC)
    event = envelope_for_delivery(
        event_type="github.pull_request",
        source="github",
        delivery_id="publication-crash-window",
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="service", id="github-app"),
        subject=EventSubject(idea_id=1, plan_id=run.plan_id, plan_version=1),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={
            "pull_request": {
                "number": 17,
                "merged": True,
                "merge_commit_sha": merge_sha,
            },
            "repository": {"full_name": run.repository},
        },
    )

    with pytest.raises(RuntimeError, match="after publication apply"):
        await service.handle(event)
    assert store.run is not None and store.run.state == "publishing"
    assert openproject.effects == ["create_work_package", "record_audit"]

    openproject.reject_snapshot_reads = True
    outcome = await service.handle(event)
    assert outcome.outcome == "published"
    assert store.run is not None and store.run.state == "published"
    assert openproject.effects == ["create_work_package", "record_audit"]


class ReconciliationStore:
    def __init__(self, runs: tuple[PlanRun, ...]) -> None:
        self.runs = runs
        self.audit_rows: list[tuple[str, str]] = []

    def all(self) -> tuple[PlanRun, ...]:
        return self.runs

    def audit(self, **values: Any) -> None:
        self.audit_rows.append((str(values["action"]), str(values["outcome"])))


class ReconciliationOpenProjectFake:
    def __init__(self, snapshot: OpenProjectSnapshot) -> None:
        self._snapshot = snapshot
        self.config = SimpleNamespace(
            status_ids={
                "Needs Input": 15,
                "Ready": 14,
                "Blocked": 13,
                "Done": 10,
                "Superseded": 11,
                "Rejected": 12,
            }
        )
        self.states: list[tuple[int, str]] = []

    def snapshot(self) -> OpenProjectSnapshot:
        return self._snapshot

    def set_lifecycle_state(
        self,
        work_package_id: int,
        *,
        status: str | None = None,
        evidence_state: str | None = None,
    ) -> None:
        del evidence_state
        assert status is not None
        self.states.append((work_package_id, status))


class ReconciliationGitHubFake:
    def __init__(self, artifacts: dict[str, bytes]) -> None:
        self.artifacts = artifacts
        self.read_commits: list[str] = []

    async def read_immutable_artifact(self, binding: ImmutableArtifactBinding) -> bytes:
        self.read_commits.append(binding.commit_sha)
        return self.artifacts[binding.commit_sha]

    async def repository_head(self, _repository: str) -> tuple[str, str]:
        return "main", "a" * 40


@pytest.mark.asyncio
async def test_reconciliation_uses_only_latest_plan_and_superseded_blocker_stays_open() -> None:
    fixture = Path(__file__).parents[1] / "evals/fixtures/single-repository/backlog.yaml"
    loaded = load_artifact(fixture)
    base = loaded.plan.items[0]
    legacy_blocker = base.model_copy(update={"key": "legacy-blocker", "title": "Legacy blocker"})
    legacy_target = base.model_copy(
        update={
            "key": "legacy-target",
            "title": "Legacy target",
            "blocked_by": ("legacy-blocker",),
        }
    )
    current_blocker = base.model_copy(
        update={"key": "current-blocker", "title": "Current blocker"}
    )
    current_target = base.model_copy(
        update={
            "key": "current-target",
            "title": "Current target",
            "blocked_by": ("current-blocker",),
        }
    )
    version_one = loaded.plan.model_copy(
        update={"items": (legacy_blocker, legacy_target)}
    )
    version_two = loaded.plan.model_copy(
        update={
            "plan": loaded.plan.plan.model_copy(
                update={
                    "version": 2,
                    "publication_identity": "single-repository:v2",
                }
            ),
            "items": (current_blocker, current_target),
        }
    )

    def encoded(plan: object) -> bytes:
        return yaml.safe_dump(
            plan.model_dump(mode="json"),  # type: ignore[attr-defined]
            sort_keys=False,
        ).encode()

    raw_one = encoded(version_one)
    raw_two = encoded(version_two)
    commit_one = "c" * 40
    commit_two = "d" * 40

    def published_run(version: int, commit: str, raw: bytes) -> PlanRun:
        return PlanRun(
            idea_id=1,
            plan_id="single-repository",
            plan_version=version,
            thread_id=f"openproject:1:planning:{version}",
            repository="Acme/service",
            base_branch="a" * 40,
            artifact_prefix=f"planning/single-repository/v{version}",
            backlog_path=f"planning/single-repository/v{version}/backlog.yaml",
            snapshot_sha256="b" * 64,
            snapshot_etag="fixture",
            state="published",
            approved_commit=commit,
            backlog_blob_sha1=hashlib.sha1(
                f"blob {len(raw)}\0".encode() + raw
            ).hexdigest(),
            backlog_sha256=hashlib.sha256(raw).hexdigest(),
        )

    runs = (
        published_run(1, commit_one, raw_one),
        published_run(2, commit_two, raw_two),
    )
    snapshot = OpenProjectSnapshot(
        captured_at="2026-07-30T00:00:00Z",
        etag="fixture",
        sha256="b" * 64,
        work_packages=(
            WorkPackageSnapshot(
                1,
                1,
                "single-repository",
                "legacy-blocker",
                1,
                human_fields={"status_id": 9},
            ),
            WorkPackageSnapshot(
                2,
                1,
                "single-repository",
                "legacy-target",
                1,
                human_fields={"status_id": 14},
            ),
            WorkPackageSnapshot(
                3,
                1,
                "single-repository",
                "current-blocker",
                2,
                human_fields={"status_id": 11},
                superseded=True,
            ),
            WorkPackageSnapshot(
                4,
                1,
                "single-repository",
                "current-target",
                2,
                human_fields={"status_id": 14},
            ),
        ),
    )
    openproject = ReconciliationOpenProjectFake(snapshot)
    github = ReconciliationGitHubFake({commit_one: raw_one, commit_two: raw_two})
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        store=ReconciliationStore(runs),  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
    )
    now = datetime(2026, 7, 30, 21, 0, tzinfo=UTC)
    event = envelope_for_delivery(
        event_type="reconciliation.scheduled",
        source="scheduler",
        delivery_id="latest-version-only",
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="system", id="windmill"),
        subject=EventSubject(),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={"schedule": "nightly"},
    )

    outcome = await service.handle(event)
    assert outcome.outcome.startswith("inspected:1:")
    assert github.read_commits == [commit_two]
    assert openproject.states == [(4, "Blocked")]
