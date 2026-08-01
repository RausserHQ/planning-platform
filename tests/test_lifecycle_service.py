from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import yaml

from planning_platform.github_adapter import GitHubAdapterError
from planning_platform.github_models import (
    CheckEvidence,
    ImmutableArtifactBinding,
    PullRequestEvidence,
    ReviewEvidence,
)
from planning_platform.lifecycle.models import (
    EventActor,
    EventSubject,
    VerifiedSignature,
    envelope_for_delivery,
)
from planning_platform.lifecycle.planner_client import PlannerThreadNotFound
from planning_platform.lifecycle.recovery import RecoveryCipher, RecoveryPayloadRejected
from planning_platform.lifecycle.service import LifecycleEventRejected, LifecycleService
from planning_platform.lifecycle.store import (
    ImplementationPrAssociation,
    LifecycleStoreMismatch,
    PlanRun,
)
from planning_platform.loader import LoadedArtifact, load_artifact, load_artifact_bytes
from planning_platform.models import BacklogPlan, with_approved_commit
from planning_platform.openproject import OpenProjectSnapshot, WorkPackageSnapshot, managed_hash
from planning_platform.planner.models import (
    ArtifactBundle,
    ArtifactContent,
    ArtifactManifestEntry,
    IdeaSnapshot,
    OpenProjectSnapshotInput,
    PlannerEvent,
    PlanResponse,
    RepositoryFile,
    RepositorySnapshot,
    ResumePlanRequest,
    StartPlanRequest,
    idea_snapshot_digest,
    repository_snapshot_digest,
)
from planning_platform.publication_journal import InMemoryPublicationJournal
from planning_platform.replan import (
    apply_replan_boundary,
    build_replan_scope,
    effective_node_binding,
)

_PLANNING_REPOSITORY = "RausserHQ/planning-platform"
_IMPLEMENTATION_CHECKS = {"Acme/service": {"implementation-tests"}}


def _review(
    head_sha: str,
    *,
    state: str = "APPROVED",
    review_id: int = 1,
) -> ReviewEvidence:
    return ReviewEvidence.model_validate(
        {
            "id": review_id,
            "actor_id": 7,
            "actor_login": "reviewer",
            "state": state,
            "submitted_at": "2026-07-30T19:00:00Z",
            "commit_sha": head_sha,
        }
    )


def _check(
    name: str,
    head_sha: str,
    *,
    conclusion: str = "success",
    check_id: int = 1,
    app_id: int = 15368,
    app_slug: str = "github-actions",
) -> CheckEvidence:
    return CheckEvidence(
        id=check_id,
        name=name,
        head_sha=head_sha,
        app_id=app_id,
        app_slug=app_slug,
        status="completed",
        conclusion=conclusion,
    )


def _service_policy() -> dict[str, object]:
    return {
        "planning_repository": _PLANNING_REPOSITORY,
        "implementation_required_checks": _IMPLEMENTATION_CHECKS,
    }


def _implementation_artifact():
    loaded = load_artifact(
        Path(__file__).parents[1] / "evals/fixtures/single-repository/backlog.yaml"
    )
    data = loaded.plan.model_dump(mode="json")
    data["plan"].update(
        {
            "id": "idea-42",
            "publication_identity": "idea-42:v1",
            "source_idea": {
                "work_package_id": 42,
                "lock_version": 1,
                "updated_at": "2026-07-30T19:00:00Z",
            },
        }
    )
    raw = yaml.safe_dump(data, sort_keys=False).encode()
    return load_artifact_bytes(raw)


def _implementation_published_run(
    *,
    artifact: LoadedArtifact | None = None,
    approved_commit: str = "c" * 40,
) -> PlanRun:
    bound = _implementation_artifact() if artifact is None else artifact
    plan = bound.plan
    return PlanRun(
        idea_id=42,
        plan_id=plan.plan.id,
        plan_version=plan.plan.version,
        thread_id=f"openproject:42:planning:{plan.plan.version}",
        repository=_PLANNING_REPOSITORY,
        base_branch="main",
        artifact_prefix=f"planning/idea-42/v{plan.plan.version}",
        backlog_path=f"planning/idea-42/v{plan.plan.version}/backlog.yaml",
        snapshot_sha256="b" * 64,
        snapshot_etag="fixture",
        state="published",
        approved_commit=approved_commit,
        approval_evidence_sha256="d" * 64,
        backlog_blob_sha1=bound.blob_sha1,
        backlog_sha256=bound.sha256,
    )


class MemoryStore:
    def __init__(self, run: PlanRun | None = None, *, crash_on_publish: bool = False) -> None:
        self.run = run
        self.audit_rows: list[tuple[str, str]] = []
        self.crash_on_publish = crash_on_publish
        self.implementation: dict[tuple[str, int], ImplementationPrAssociation] = {}

    def latest_for_idea(self, _idea_id: int) -> PlanRun | None:
        return self.run

    def all(self) -> tuple[PlanRun, ...]:
        return () if self.run is None else (self.run,)

    def get(self, plan_id: str, plan_version: int) -> PlanRun | None:
        if (
            self.run is not None
            and self.run.plan_id == plan_id
            and self.run.plan_version == plan_version
        ):
            return self.run
        return None

    def stale_thread_count(self, _cutoff: datetime) -> int:
        return 0

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

    def approve_for_publication(
        self,
        run: PlanRun,
        *,
        merge_commit: str,
        evidence_sha256: str,
    ) -> PlanRun:
        self.run = replace(
            run,
            state="publishing",
            approved_commit=merge_commit,
            approval_evidence_sha256=evidence_sha256,
        )
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
        key = repository, number
        current = self.implementation.get(key)
        if current is None:
            current = ImplementationPrAssociation(
                repository=repository,
                pull_request_number=number,
                plan_id=plan_id,
                node_key=node_key,
                work_package_id=work_package_id,
                pull_request_url=url,
                head_sha=head_sha,
                head_observed_at=observed_at,
                pull_request_state=pull_request_state,  # type: ignore[arg-type]
                merged_commit=merged_commit,
            )
        elif (
            current.plan_id,
            current.node_key,
            current.work_package_id,
            current.pull_request_url,
        ) != (plan_id, node_key, work_package_id, url):
            raise LifecycleStoreMismatch(
                "implementation PR replay changed its work-package identity"
            )
        elif (
            current.merged_commit is None
            and observed_at > current.head_observed_at
            and (head_sha != current.head_sha or pull_request_state != current.pull_request_state)
        ):
            current = replace(
                current,
                head_sha=head_sha,
                head_observed_at=observed_at,
                pull_request_state=pull_request_state,  # type: ignore[arg-type]
                successful_check_sha=(
                    current.successful_check_sha if head_sha == current.head_sha else None
                ),
            )
        if merged_commit is not None:
            if current.merged_commit is not None and current.merged_commit != merged_commit:
                raise LifecycleStoreMismatch("implementation PR replay changed its merge commit")
            current = replace(
                current,
                pull_request_state="closed",
                merged_commit=merged_commit,
            )
        self.implementation[key] = current
        return current

    def record_implementation_check_result(
        self,
        repository: str,
        number: int,
        *,
        head_sha: str,
        passed: bool,
    ) -> ImplementationPrAssociation | None:
        key = repository, number
        current = self.implementation.get(key)
        if current is None or current.head_sha != head_sha:
            return None
        current = replace(
            current,
            successful_check_sha=head_sha if passed else None,
        )
        self.implementation[key] = current
        return current

    def by_implementation_pull_request(
        self,
        repository: str,
        number: int,
    ) -> ImplementationPrAssociation | None:
        return self.implementation.get((repository, number))

    def implementation_associations(self) -> tuple[ImplementationPrAssociation, ...]:
        return tuple(self.implementation.values())

    def latest_published(self, plan_id: str) -> PlanRun | None:
        if self.run is not None and self.run.plan_id == plan_id and self.run.state == "published":
            return self.run
        if plan_id == "idea-42":
            return _implementation_published_run()
        return None

    def clear_pending_resume(self, run: PlanRun, _request_ciphertext: str) -> PlanRun:
        assert self.run is not None
        self.run = replace(self.run, pending_resume_ciphertext=None)
        return self.run

    def audit(self, **values: Any) -> None:
        self.audit_rows.append((str(values["action"]), str(values["outcome"])))


class OpenProjectFake:
    def __init__(self) -> None:
        self.publication_target_sha256 = "e" * 64
        self.states: list[tuple[int, str | None, str | None]] = []
        self.comments: list[str] = []
        self.config = SimpleNamespace(
            status_ids={
                "Draft": 1,
                "Planning": 2,
                "Needs Input": 3,
                "Proposed": 4,
                "Ready": 5,
                "In Progress": 6,
                "Blocked": 7,
                "Review": 8,
                "Done": 9,
                "Superseded": 10,
                "Rejected": 11,
            }
        )
        self._status_id = 5
        self._evidence_state: str | None = None
        self.repository = "Acme/service"
        self.plan_version = 1
        self.managed_hash_value: str | None = None

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
        if status is not None:
            self._status_id = self.config.status_ids[status]
        if evidence_state is not None:
            self._evidence_state = evidence_state

    def resolve(self, identity: tuple[str, str]) -> WorkPackageSnapshot:
        return WorkPackageSnapshot(
            id=101,
            lock_version=1,
            plan_id=identity[0],
            node_key=identity[1],
            plan_version=self.plan_version,
            repository=self.repository,
            managed_hash=self.managed_hash_value,
            human_fields={"status_id": self._status_id},
            evidence_state=self._evidence_state,
        )


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

    async def repository_head(self, _repository: str) -> tuple[str, str]:
        return "main", "f" * 40


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


def _sealed_ordinary_start_request(backlog: BacklogPlan, run: PlanRun) -> str:
    reference = backlog.plan.openproject_snapshot
    snapshot = OpenProjectSnapshotInput(
        captured_at=datetime.fromisoformat(reference.captured_at.replace("Z", "+00:00")),
        etag=run.snapshot_etag,
        sha256=run.snapshot_sha256,
    )
    source = backlog.plan.source_idea
    idea = IdeaSnapshot(
        work_package_id=run.idea_id,
        lock_version=source.lock_version,
        updated_at=datetime.fromisoformat(source.updated_at.replace("Z", "+00:00")),
        title="Publication fixture",
    )
    repository = backlog.plan.repositories[0]
    repository_snapshot = RepositorySnapshot(
        name=repository.name,
        commit=repository.commit,
        snapshot_sha256=repository_snapshot_digest(
            repository.name,
            repository.commit,
            (),
        ),
        files=(),
    )
    request = StartPlanRequest(
        event=PlannerEvent(idempotency_key="publication-fixture", trace_id=uuid4()),
        idea=idea,
        plan_id=run.plan_id,
        plan_version=run.plan_version,
        idea_sha256=idea_snapshot_digest(idea, snapshot),
        openproject_snapshot=snapshot,
        repositories=(repository_snapshot,),
    )
    return _cipher().seal(
        purpose="planner-start",
        binding=run.thread_id,
        plaintext=request.model_dump_json(),
    )


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
        **_service_policy(),
    )
    event = _work_package_event()
    with pytest.raises(RuntimeError, match="simulated"):
        await service.handle(event)
    assert store.run is not None
    assert store.run.start_request_ciphertext.startswith("v1:")
    assert "Recover crash windows" not in store.run.start_request_ciphertext
    assert "# Planning Platform" not in store.run.start_request_ciphertext
    assert store.run.repository == _PLANNING_REPOSITORY
    assert store.run.base_branch == "main"

    outcome = await service.handle(event)
    assert outcome.outcome == "failed"
    assert len(planner.starts) == 2
    assert planner.starts[0] == planner.starts[1]
    assert store.run is not None and store.run.state == "failed"


class VersionedReplanStore(MemoryStore):
    def __init__(self, base: PlanRun) -> None:
        super().__init__(base)
        self.runs = {(base.plan_id, base.plan_version): base}

    def get(self, plan_id: str, plan_version: int) -> PlanRun | None:
        return self.runs.get((plan_id, plan_version))

    def latest_for_idea(self, idea_id: int) -> PlanRun | None:
        matches = [run for run in self.runs.values() if run.idea_id == idea_id]
        return max(matches, key=lambda run: run.plan_version, default=None)

    def latest_published(self, plan_id: str) -> PlanRun | None:
        matches = [
            run for run in self.runs.values() if run.plan_id == plan_id and run.state == "published"
        ]
        return max(matches, key=lambda run: run.plan_version, default=None)

    def begin(self, run: PlanRun) -> PlanRun:
        stored = self.runs.setdefault((run.plan_id, run.plan_version), run)
        self.run = stored
        return stored

    def set_state(self, run: PlanRun, state: str) -> PlanRun:
        updated = replace(run, state=state)
        self.runs[(run.plan_id, run.plan_version)] = updated
        self.run = updated
        return updated


class ReplanGitHubFake(GitHubContextFake):
    def __init__(self, raw: bytes, binding: ImmutableArtifactBinding) -> None:
        self.raw = raw
        self.binding = binding

    async def read_immutable_artifact(self, binding: ImmutableArtifactBinding) -> bytes:
        assert binding == self.binding
        return self.raw

    async def ensure_planning_commit(self, **_values: object) -> None:
        raise AssertionError("out-of-scope replan artifact reached a GitHub side effect")


class ReplanOpenProjectFake(OpenProjectFake):
    def read_work_package(self, work_package_id: int) -> dict[str, Any]:
        value = super().read_work_package(work_package_id)
        value["description"] = {"raw": "## Relevant repositories\nAcme/service"}
        return value


class OutOfScopeReplanPlanner:
    def __init__(self) -> None:
        self.bundle: ArtifactBundle | None = None

    async def start(self, request: StartPlanRequest) -> PlanResponse:
        assert request.replan is not None
        prior = request.replan.prior_plan
        root, protected = prior.items
        scope = build_replan_scope(
            prior,
            base_approved_commit=request.replan.base_approved_planning_commit,
            selected_root_keys=request.replan.selected_root_keys,
            affected_node_keys=request.replan.affected_node_keys,
        )
        data = prior.model_dump(mode="json")
        data["plan"].update(
            {
                "version": request.plan_version,
                "publication_identity": f"{request.plan_id}:v{request.plan_version}",
                "source_idea": {
                    "work_package_id": request.idea.work_package_id,
                    "lock_version": request.idea.lock_version,
                    "updated_at": request.idea.updated_at.isoformat(),
                },
                "repositories": [
                    {"name": repository.name, "commit": repository.commit}
                    for repository in request.repositories
                ],
                "approved_planning_commit": None,
                "openproject_snapshot": request.openproject_snapshot.model_dump(mode="json"),
                "replan": scope.model_dump(mode="json"),
            }
        )
        data["items"] = [
            root.model_dump(mode="json"),
            protected.model_copy(update={"title": "Escaped rewrite"}).model_dump(mode="json"),
        ]
        plan = type(prior).model_validate(data)
        content = yaml.safe_dump(plan.model_dump(mode="json"), sort_keys=False)
        digest = hashlib.sha256(content.encode()).hexdigest()
        self.bundle = ArtifactBundle(
            thread_id=f"openproject:{request.idea.work_package_id}:planning:{request.plan_version}",
            artifacts=(ArtifactContent(path="backlog.yaml", sha256=digest, content=content),),
        )
        return PlanResponse(
            thread_id=self.bundle.thread_id,
            status="artifacts_ready",
            trace_id=str(request.event.trace_id),
            interrupt=None,
            artifact_manifest=(ArtifactManifestEntry(path="backlog.yaml", sha256=digest),),
        )

    async def artifacts(self, thread_id: str) -> ArtifactBundle:
        assert self.bundle is not None and self.bundle.thread_id == thread_id
        return self.bundle


def _replan_event(
    *,
    source: str = "windmill",
    delivery_id: str = "replan:wm-root-job-1176",
):
    now = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
    return envelope_for_delivery(
        event_type="planning.replan_affected_subgraph",
        source=source,
        delivery_id=delivery_id,
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="system", id="windmill-operator"),
        subject=EventSubject(plan_id="single-repository", plan_version=1),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={
            "plan_id": "single-repository",
            "base_plan_version": 1,
            "affected_node_keys": ["implement-core"],
            "reason": "Refine only the implementation branch.",
        },
    )


@pytest.mark.asyncio
async def test_bounded_replan_recovers_exact_request_after_successor_insert() -> None:
    loaded = load_artifact(
        Path(__file__).parents[1] / "evals/fixtures/single-repository/backlog.yaml"
    )
    root = loaded.plan.items[0]
    child = root.model_copy(update={"key": "implement-child", "parent": root.key})
    protected = root.model_copy(update={"key": "protected-node", "title": "Protected node"})
    prior = loaded.plan.model_copy(update={"items": (root, child, protected)})
    raw = yaml.safe_dump(prior.model_dump(mode="json"), sort_keys=False).encode()
    artifact = load_artifact_bytes(raw)
    base = PlanRun(
        idea_id=1,
        plan_id=prior.plan.id,
        plan_version=1,
        thread_id="openproject:1:planning:1",
        repository=_PLANNING_REPOSITORY,
        base_branch="main",
        artifact_prefix="planning/single-repository/v1",
        backlog_path="planning/single-repository/v1/backlog.yaml",
        snapshot_sha256="b" * 64,
        snapshot_etag="fixture",
        state="published",
        approved_commit="c" * 40,
        approval_evidence_sha256="d" * 64,
        backlog_blob_sha1=artifact.blob_sha1,
        backlog_sha256=artifact.sha256,
    )
    binding = ImmutableArtifactBinding(
        repository=base.repository,
        commit_sha=base.approved_commit,
        path=base.backlog_path,
        blob_sha=base.backlog_blob_sha1,
        content_sha256=base.backlog_sha256,
    )
    store = VersionedReplanStore(base)
    planner = CrashThenFailPlanner()
    openproject = ReplanOpenProjectFake()
    service = LifecycleService(
        planner=planner,  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=ReplanGitHubFake(raw, binding),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )
    event = _replan_event()

    with pytest.raises(RuntimeError, match="simulated"):
        await service.handle(event)
    successor = store.get(prior.plan.id, 2)
    assert successor is not None
    assert successor.start_request_ciphertext.startswith("v1:")

    outcome = await service.handle(event)

    assert outcome.outcome == "failed"
    assert len(planner.starts) == 2
    assert planner.starts[0] == planner.starts[1]
    request = planner.starts[0]
    assert isinstance(request, StartPlanRequest)
    assert request.replan is not None
    assert request.replan.affected_node_keys == (root.key, child.key)
    assert request.replan.base_approved_planning_commit == "c" * 40
    proposal = prior.model_copy(
        update={
            "plan": prior.plan.model_copy(
                update={"version": 2, "publication_identity": f"{prior.plan.id}:v2"}
            ),
            "items": (
                root.model_copy(update={"title": "Authorized root change"}),
                child,
                protected,
            ),
        }
    )
    bounded = apply_replan_boundary(
        prior,
        proposal,
        base_approved_commit="c" * 40,
        selected_root_keys=(root.key,),
        affected_node_keys=(root.key, child.key),
    )
    bounded_artifact = load_artifact_bytes(
        yaml.safe_dump(bounded.model_dump(mode="json"), sort_keys=False).encode()
    )
    failed_successor = store.get(prior.plan.id, 2)
    assert failed_successor is not None
    publication_context = await service._replan_publication_context(
        failed_successor,
        bounded_artifact,
    )
    assert publication_context is not None
    assert publication_context.selected_root_keys == (root.key,)
    assert publication_context.affected_node_keys == (root.key, child.key)
    unscoped = bounded.model_copy(
        update={"plan": bounded.plan.model_copy(update={"replan": None})}
    )
    unscoped_artifact = load_artifact_bytes(
        yaml.safe_dump(unscoped.model_dump(mode="json"), sort_keys=False).encode()
    )
    with pytest.raises(LifecycleEventRejected, match="operator authorization"):
        await service._replan_publication_context(failed_successor, unscoped_artifact)
    assert store.latest_for_idea(1).state == "failed"  # type: ignore[union-attr]
    assert openproject.states.count((1, "Planning", None)) == 2

    with pytest.raises(LifecycleEventRejected, match="Windmill internal"):
        await service.handle(_replan_event(source="github"))

    ordinary = _work_package_event().model_copy(
        update={
            "subject": EventSubject(idea_id=1),
            "payload": {
                "work_package": {
                    "id": 1,
                    "_links": {
                        "type": {"title": "Idea"},
                        "status": {"title": "Planning"},
                    },
                }
            },
        }
    )
    ignored = await service.handle(ordinary)
    assert ignored.outcome == "ignored_after_failed"
    assert len(store.runs) == 2

    retry = await service.handle(
        _replan_event(delivery_id="replan:wm-root-job-1176-retry")
    )
    assert retry.outcome == "failed"
    assert store.get(prior.plan.id, 3).state == "failed"  # type: ignore[union-attr]
    assert len(store.runs) == 3


@pytest.mark.asyncio
async def test_lifecycle_rejects_planner_artifact_outside_replan_scope_before_github() -> None:
    loaded = load_artifact(
        Path(__file__).parents[1] / "evals/fixtures/single-repository/backlog.yaml"
    )
    root = loaded.plan.items[0]
    protected = root.model_copy(update={"key": "protected-node", "title": "Protected node"})
    prior = loaded.plan.model_copy(update={"items": (root, protected)})
    raw = yaml.safe_dump(prior.model_dump(mode="json"), sort_keys=False).encode()
    artifact = load_artifact_bytes(raw)
    base = PlanRun(
        idea_id=1,
        plan_id=prior.plan.id,
        plan_version=1,
        thread_id="openproject:1:planning:1",
        repository=_PLANNING_REPOSITORY,
        base_branch="main",
        artifact_prefix="planning/single-repository/v1",
        backlog_path="planning/single-repository/v1/backlog.yaml",
        snapshot_sha256="b" * 64,
        snapshot_etag="fixture",
        state="published",
        approved_commit="c" * 40,
        approval_evidence_sha256="d" * 64,
        backlog_blob_sha1=artifact.blob_sha1,
        backlog_sha256=artifact.sha256,
    )
    binding = ImmutableArtifactBinding(
        repository=base.repository,
        commit_sha=base.approved_commit,
        path=base.backlog_path,
        blob_sha=base.backlog_blob_sha1,
        content_sha256=base.backlog_sha256,
    )
    service = LifecycleService(
        planner=OutOfScopeReplanPlanner(),  # type: ignore[arg-type]
        openproject=ReplanOpenProjectFake(),  # type: ignore[arg-type]
        github=ReplanGitHubFake(raw, binding),  # type: ignore[arg-type]
        store=VersionedReplanStore(base),  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )

    with pytest.raises(LifecycleEventRejected, match="escaped bounded replan scope"):
        await service.handle(_replan_event())


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
        **_service_policy(),
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


@pytest.mark.asyncio
async def test_service_comment_cannot_replay_a_pending_human_resume() -> None:
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
    ciphertext = _cipher().seal(
        purpose="planner-resume",
        binding="openproject:42:planning:1",
        plaintext=request.model_dump_json(),
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
        pending_resume_ciphertext=ciphertext,
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
        **_service_policy(),
    )
    now = datetime(2026, 7, 30, 19, 1, tzinfo=UTC)
    event = envelope_for_delivery(
        event_type="openproject.idea_comment",
        source="windmill",
        delivery_id="service-comment",
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="service", id="7"),
        subject=EventSubject(idea_id=42),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={
            "work_package_comment": {
                "id": 20,
                "createdAt": "2026-07-30T19:01:00Z",
                "comment": {
                    "raw": (
                        "Planning input required.\n\n"
                        "<!-- planning-platform:comment:"
                        "interrupt:openproject:42:planning:1:interrupt-1 -->"
                    )
                },
                "_links": {"workPackage": {"href": "/api/v3/work_packages/42"}},
            }
        },
    )

    outcome = await service.handle(event)

    assert outcome.outcome == "ignored_service_comment"
    assert planner.requests == []
    assert store.run is not None
    assert store.run.state == "needs_input"
    assert store.run.pending_resume_ciphertext == ciphertext


def _implementation_pr_event(
    *,
    delivery: str,
    occurred_at: datetime,
    node_key: str = "implement-core",
    merged: bool = False,
    head_sha: str = "d" * 40,
    state: str | None = None,
    number: int = 23,
    repository: str = "Acme/service",
):
    return envelope_for_delivery(
        event_type="github.pull_request",
        source="github",
        delivery_id=delivery,
        occurred_at=occurred_at,
        received_at=occurred_at,
        actor=EventActor(kind="service", id="github-app"),
        subject=EventSubject(),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={
            "pull_request": {
                "number": number,
                "body": (
                    f"<!-- planning-platform:work-package plan_id=idea-42 node_key={node_key} -->"
                ),
                "html_url": f"https://github.com/{repository}/pull/{number}",
                "head": {"sha": head_sha},
                "state": state or ("closed" if merged else "open"),
                "merged": merged,
                "merge_commit_sha": "e" * 40 if merged else None,
            },
            "repository": {"full_name": repository},
        },
    )


def _implementation_check_event(
    *,
    delivery: str,
    occurred_at: datetime,
    conclusion: str = "success",
    head_sha: str = "d" * 40,
    number: int = 23,
    repository: str = "Acme/service",
):
    return envelope_for_delivery(
        event_type="github.check_run",
        source="github",
        delivery_id=delivery,
        occurred_at=occurred_at,
        received_at=occurred_at,
        actor=EventActor(kind="service", id="github-app"),
        subject=EventSubject(),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={
            "check_run": {
                "id": 501,
                "name": "implementation-tests",
                "app": {"id": 15368, "slug": "github-actions"},
                "status": "completed",
                "conclusion": conclusion,
                "head_sha": head_sha,
                "pull_requests": [{"number": number}],
            },
            "repository": {"full_name": repository},
        },
    )


class ImplementationGitHubFake:
    def __init__(self, artifact: LoadedArtifact | None = None) -> None:
        self.conclusion = "success"
        self.check_name = "implementation-tests"
        self.app_slug = "github-actions"
        self.state = "open"
        self.artifact = artifact or _implementation_artifact()

    async def read_immutable_artifact(self, binding: ImmutableArtifactBinding) -> bytes:
        assert binding.blob_sha == self.artifact.blob_sha1
        assert binding.content_sha256 == self.artifact.sha256
        return self.artifact.raw_bytes

    async def pull_request_evidence(
        self,
        repository: str,
        number: int,
        *,
        expected_head_sha: str | None = None,
    ) -> PullRequestEvidence:
        head_sha = expected_head_sha or "d" * 40
        return PullRequestEvidence(
            repository=repository,
            number=number,
            head_sha=head_sha,
            state=self.state,  # type: ignore[arg-type]
            merged=False,
            merge_commit_sha=None,
            reviews=(),
            checks=(
                _check(
                    self.check_name,
                    head_sha,
                    conclusion=self.conclusion,
                    app_slug=self.app_slug,
                ),
            ),
        )


def _implementation_openproject(
    artifact: LoadedArtifact | None = None,
    *,
    approved_commit: str = "c" * 40,
) -> OpenProjectFake:
    bound = artifact or _implementation_artifact()
    item = bound.plan.by_key["implement-core"]
    version, _commit = effective_node_binding(bound.plan, approved_commit, item.key)
    openproject = OpenProjectFake()
    openproject.plan_version = version
    openproject.managed_hash_value = managed_hash(
        with_approved_commit(bound.plan, approved_commit),
        item,
    )
    return openproject


@pytest.mark.asyncio
async def test_protected_prior_version_node_accepts_new_implementation_pr() -> None:
    base_artifact = _implementation_artifact()
    core = base_artifact.plan.by_key["implement-core"]
    affected = core.model_copy(update={"key": "affected-node", "title": "Affected node"})
    base = base_artifact.plan.model_copy(update={"items": (affected, core)})
    proposal = base.model_copy(
        update={
            "plan": base.plan.model_copy(
                update={"version": 2, "publication_identity": "idea-42:v2"}
            ),
            "items": (
                affected.model_copy(update={"title": "Affected node v2"}),
                core,
            ),
        }
    )
    version_two = apply_replan_boundary(
        base,
        proposal,
        base_approved_commit="c" * 40,
        selected_root_keys=(affected.key,),
        affected_node_keys=(affected.key,),
    )
    artifact = load_artifact_bytes(
        yaml.safe_dump(version_two.model_dump(mode="json"), sort_keys=False).encode()
    )
    published = _implementation_published_run(
        artifact=artifact,
        approved_commit="e" * 40,
    )
    store = MemoryStore(published)
    openproject = _implementation_openproject(artifact, approved_commit="e" * 40)
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=ImplementationGitHubFake(artifact),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )

    outcome = await service.handle(
        _implementation_pr_event(
            delivery="protected-node-implementation-open",
            occurred_at=datetime(2026, 7, 30, 19, 1, tzinfo=UTC),
        )
    )

    assert outcome.outcome == "open"
    assert store.by_implementation_pull_request("Acme/service", 23) is not None


@pytest.mark.asyncio
async def test_implementation_pr_and_check_events_converge_through_durable_mapping() -> None:
    store = MemoryStore()
    openproject = _implementation_openproject()
    github = ImplementationGitHubFake()
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )
    opened_at = datetime(2026, 7, 30, 19, 1, tzinfo=UTC)

    opened = await service.handle(
        _implementation_pr_event(delivery="implementation-open", occurred_at=opened_at)
    )
    association = store.by_implementation_pull_request("Acme/service", 23)
    assert opened.outcome == "open"
    assert association is not None
    assert (association.plan_id, association.node_key, association.work_package_id) == (
        "idea-42",
        "implement-core",
        101,
    )
    assert openproject.states[-1] == (101, "In Progress", "pr_open")
    assert openproject.comments[-1].endswith("https://github.com/Acme/service/pull/23")

    checked = await service.handle(
        _implementation_check_event(
            delivery="implementation-check",
            occurred_at=opened_at.replace(minute=2),
        )
    )
    assert checked.outcome == "successful:1"
    assert openproject.states[-1] == (101, "Review", "check_passed")

    state_count = len(openproject.states)
    second = await service.handle(
        _implementation_pr_event(
            delivery="implementation-second-open",
            occurred_at=opened_at.replace(minute=3),
            number=24,
        )
    )
    assert second.outcome == "open"
    assert len(openproject.states) == state_count
    assert openproject._status_id == openproject.config.status_ids["Review"]

    openproject._status_id = openproject.config.status_ids["Done"]
    merged = await service.handle(
        _implementation_pr_event(
            delivery="implementation-merge",
            occurred_at=opened_at.replace(minute=4),
            merged=True,
        )
    )
    assert merged.outcome == "merged"
    assert openproject.states[-1] == (101, None, "pr_merged")
    assert openproject._status_id == openproject.config.status_ids["Done"]

    reordered = await service.handle(
        _implementation_pr_event(
            delivery="implementation-late-open",
            occurred_at=opened_at.replace(minute=2),
        )
    )
    assert reordered.outcome == "merged"
    assert openproject.states[-1] == (101, None, "pr_merged")
    assert openproject._status_id == openproject.config.status_ids["Done"]


@pytest.mark.asyncio
async def test_implementation_mapping_rejects_retarget_and_stale_or_failed_checks() -> None:
    store = MemoryStore()
    openproject = _implementation_openproject()
    github = ImplementationGitHubFake()
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )
    now = datetime(2026, 7, 30, 19, 1, tzinfo=UTC)
    await service.handle(_implementation_pr_event(delivery="implementation-bind", occurred_at=now))
    states_before = len(openproject.states)

    stale = await service.handle(
        _implementation_check_event(
            delivery="implementation-stale-check",
            occurred_at=now.replace(minute=2),
            head_sha="f" * 40,
        )
    )
    github.conclusion = "failure"
    failed = await service.handle(
        _implementation_check_event(
            delivery="implementation-failed-check",
            occurred_at=now.replace(minute=3),
            conclusion="failure",
        )
    )
    assert stale.outcome == "successful:0"
    assert failed.outcome == "successful:0"
    assert len(openproject.states) == states_before

    with pytest.raises(LifecycleStoreMismatch, match="identity"):
        await service.handle(
            _implementation_pr_event(
                delivery="implementation-retarget",
                occurred_at=now.replace(minute=4),
                node_key="different-node",
            )
        )


@pytest.mark.asyncio
async def test_implementation_mapping_rejects_inactive_or_wrong_repository_nodes() -> None:
    store = MemoryStore()
    openproject = _implementation_openproject()
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=ImplementationGitHubFake(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )
    now = datetime(2026, 7, 30, 19, 1, tzinfo=UTC)

    closed = await service.handle(
        _implementation_pr_event(
            delivery="implementation-closed",
            occurred_at=now,
            state="closed",
        )
    )
    association = store.by_implementation_pull_request("Acme/service", 23)
    assert closed.outcome == "closed"
    assert association is not None and association.pull_request_state == "closed"
    assert openproject.states[-1] == (101, None, "pr_closed")
    assert openproject._status_id == openproject.config.status_ids["Ready"]

    openproject.repository = "Other/service"
    with pytest.raises(LifecycleEventRejected, match="active approved repository"):
        await service.handle(
            _implementation_pr_event(
                delivery="implementation-wrong-repository",
                occurred_at=now.replace(minute=2),
                number=24,
            )
        )

    openproject.repository = "Acme/service"
    openproject._status_id = openproject.config.status_ids["Superseded"]
    with pytest.raises(LifecycleEventRejected, match="active approved repository"):
        await service.handle(
            _implementation_pr_event(
                delivery="implementation-superseded",
                occurred_at=now.replace(minute=3),
                number=25,
            )
        )

    openproject._status_id = openproject.config.status_ids["Ready"]
    with pytest.raises(LifecycleEventRejected, match="active approved repository"):
        await service.handle(
            _implementation_pr_event(
                delivery="implementation-removed-node",
                occurred_at=now.replace(minute=4),
                node_key="removed-node",
                number=26,
            )
        )


@pytest.mark.asyncio
async def test_only_configured_trusted_current_checks_advance_implementation() -> None:
    store = MemoryStore()
    openproject = _implementation_openproject()
    github = ImplementationGitHubFake()
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )
    now = datetime(2026, 7, 30, 19, 1, tzinfo=UTC)
    await service.handle(_implementation_pr_event(delivery="implementation-bind", occurred_at=now))

    github.check_name = "unconfigured-check"
    wrong_name = await service.handle(
        _implementation_check_event(
            delivery="implementation-wrong-check",
            occurred_at=now.replace(minute=2),
        )
    )
    github.check_name = "implementation-tests"
    github.app_slug = "untrusted-app"
    wrong_app = await service.handle(
        _implementation_check_event(
            delivery="implementation-wrong-app",
            occurred_at=now.replace(minute=3),
        )
    )
    github.app_slug = "github-actions"
    passing = await service.handle(
        _implementation_check_event(
            delivery="implementation-trusted-check",
            occurred_at=now.replace(minute=4),
        )
    )

    assert wrong_name.outcome == "successful:0"
    assert wrong_app.outcome == "successful:0"
    assert passing.outcome == "successful:1"
    association = store.by_implementation_pull_request("Acme/service", 23)
    assert association is not None
    assert association.successful_check_sha == association.head_sha
    assert openproject._status_id == openproject.config.status_ids["Review"]


class SnapshotOpenProjectFake(OpenProjectFake):
    def snapshot(self) -> OpenProjectSnapshot:
        return OpenProjectSnapshot(
            captured_at="2026-07-30T19:00:00Z",
            etag='"snapshot-1"',
            sha256="b" * 64,
            work_packages=(self.resolve(("idea-42", "implement-core")),),
        )


@pytest.mark.asyncio
async def test_closed_implementation_pr_becomes_a_stale_evidence_finding() -> None:
    now = datetime(2026, 7, 30, 19, 1, tzinfo=UTC)
    store = MemoryStore()
    store.bind_implementation_pull_request(
        repository="Acme/service",
        number=23,
        plan_id="idea-42",
        node_key="implement-core",
        work_package_id=101,
        url="https://github.com/Acme/service/pull/23",
        head_sha="d" * 40,
        observed_at=now - timedelta(days=2),
        pull_request_state="closed",
        merged_commit=None,
    )
    openproject = SnapshotOpenProjectFake()
    openproject._status_id = openproject.config.status_ids["In Progress"]
    openproject._evidence_state = "pr_closed"
    github = ImplementationGitHubFake()
    github.state = "closed"
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        planning_repository=_PLANNING_REPOSITORY,
        implementation_required_checks=_IMPLEMENTATION_CHECKS,
        implementation_stale_after=timedelta(hours=24),
    )
    event = envelope_for_delivery(
        event_type="reconciliation.scheduled",
        source="scheduler",
        delivery_id="closed-implementation-stale",
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="system", id="windmill"),
        subject=EventSubject(),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={"schedule": "nightly"},
    )

    outcome = await service.handle(event)

    assert ":findings:1:" in outcome.outcome
    association = store.by_implementation_pull_request("Acme/service", 23)
    assert association is not None and association.pull_request_state == "closed"
    assert openproject._status_id == openproject.config.status_ids["In Progress"]


class ImplementationReconciliationOpenProjectFake(OpenProjectFake):
    pass


class ImplementationReconciliationGitHubFake:
    async def pull_request_evidence(
        self,
        repository: str,
        number: int,
        *,
        expected_head_sha: str | None = None,
    ) -> PullRequestEvidence:
        head_sha = expected_head_sha or "d" * 40
        return PullRequestEvidence(
            repository=repository,
            number=number,
            head_sha=head_sha,
            state="closed",
            merged=True,
            merge_commit_sha="e" * 40,
            reviews=(_review(head_sha),),
            checks=(_check("implementation-tests", head_sha),),
        )


@pytest.mark.asyncio
async def test_reconciliation_repairs_suppressed_implementation_merge_and_check() -> None:
    store = MemoryStore()
    opened_at = datetime(2026, 7, 30, 19, 1, tzinfo=UTC)
    store.bind_implementation_pull_request(
        repository="Acme/service",
        number=23,
        plan_id="idea-42",
        node_key="implement-core",
        work_package_id=101,
        url="https://github.com/Acme/service/pull/23",
        head_sha="d" * 40,
        observed_at=opened_at,
        pull_request_state="open",
        merged_commit=None,
    )
    openproject = ImplementationReconciliationOpenProjectFake()
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=ImplementationReconciliationGitHubFake(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )
    event = envelope_for_delivery(
        event_type="reconciliation.scheduled",
        source="scheduler",
        delivery_id="repair-suppressed-implementation",
        occurred_at=opened_at.replace(minute=5),
        received_at=opened_at.replace(minute=5),
        actor=EventActor(kind="system", id="windmill"),
        subject=EventSubject(),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={"schedule": "nightly"},
    )

    outcome = await service.handle(event)

    association = store.by_implementation_pull_request("Acme/service", 23)
    assert association is not None
    assert association.merged_commit == "e" * 40
    assert association.successful_check_sha == "d" * 40
    assert openproject.states[-1] == (101, "Review", "pr_merged")
    assert "implementation_repairs:1" in outcome.outcome


class PartialImplementationReconciliationGitHubFake:
    async def pull_request_evidence(
        self,
        repository: str,
        number: int,
        *,
        expected_head_sha: str | None = None,
    ) -> PullRequestEvidence:
        del expected_head_sha
        if number == 23:
            raise GitHubAdapterError("repository is temporarily inaccessible")
        head_sha = "f" * 40
        return PullRequestEvidence(
            repository=repository,
            number=number,
            head_sha=head_sha,
            state="open",
            merged=False,
            merge_commit_sha=None,
            reviews=(),
            checks=(_check("implementation-tests", head_sha),),
        )


@pytest.mark.asyncio
async def test_reconciliation_repairs_projection_and_missed_head_without_global_abort() -> None:
    store = MemoryStore()
    observed_at = datetime(2026, 7, 28, 19, 1, tzinfo=UTC)
    for number, head_sha in ((23, "d" * 40), (24, "e" * 40)):
        store.bind_implementation_pull_request(
            repository="Acme/service",
            number=number,
            plan_id="idea-42",
            node_key="implement-core",
            work_package_id=101,
            url=f"https://github.com/Acme/service/pull/{number}",
            head_sha=head_sha,
            observed_at=observed_at,
            pull_request_state="open",
            merged_commit=None,
        )
    store.record_implementation_check_result(
        "Acme/service",
        24,
        head_sha="e" * 40,
        passed=True,
    )
    openproject = ImplementationReconciliationOpenProjectFake()
    openproject._status_id = openproject.config.status_ids["In Progress"]
    openproject._evidence_state = "pr_open"
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=PartialImplementationReconciliationGitHubFake(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )
    now = datetime(2026, 7, 30, 19, 1, tzinfo=UTC)
    event = envelope_for_delivery(
        event_type="reconciliation.scheduled",
        source="scheduler",
        delivery_id="partial-implementation-repair",
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="system", id="windmill"),
        subject=EventSubject(),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={"schedule": "nightly"},
    )

    outcome = await service.handle(event)

    repaired = store.by_implementation_pull_request("Acme/service", 24)
    assert repaired is not None
    assert repaired.head_sha == "f" * 40
    assert repaired.successful_check_sha == "f" * 40
    assert openproject.states[-1] == (101, "Review", "check_passed")
    assert "implementation_repairs:1" in outcome.outcome
    assert "implementation_failures:1" in outcome.outcome


@pytest.mark.asyncio
async def test_reconciliation_isolates_invalid_projection_before_valid_work_package() -> None:
    store = MemoryStore()
    observed_at = datetime(2026, 7, 30, 19, 1, tzinfo=UTC)
    store.bind_implementation_pull_request(
        repository="Acme/service",
        number=22,
        plan_id="idea-42",
        node_key="stale-node",
        work_package_id=100,
        url="https://github.com/Acme/service/pull/22",
        head_sha="c" * 40,
        observed_at=observed_at,
        pull_request_state="open",
        merged_commit=None,
    )
    store.bind_implementation_pull_request(
        repository="Acme/service",
        number=24,
        plan_id="idea-42",
        node_key="implement-core",
        work_package_id=101,
        url="https://github.com/Acme/service/pull/24",
        head_sha="d" * 40,
        observed_at=observed_at,
        pull_request_state="open",
        merged_commit=None,
    )
    openproject = ImplementationReconciliationOpenProjectFake()
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=ImplementationReconciliationGitHubFake(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )
    event = envelope_for_delivery(
        event_type="reconciliation.scheduled",
        source="scheduler",
        delivery_id="isolate-invalid-projection",
        occurred_at=observed_at.replace(minute=5),
        received_at=observed_at.replace(minute=5),
        actor=EventActor(kind="system", id="windmill"),
        subject=EventSubject(),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={"schedule": "nightly"},
    )

    outcome = await service.handle(event)

    assert openproject.states[-1] == (101, "Review", "pr_merged")
    assert "implementation_repairs:1" in outcome.outcome
    assert "implementation_failures:1" in outcome.outcome


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
    def __init__(
        self,
        raw: bytes,
        binding: ImmutableArtifactBinding,
        *,
        evidence: PullRequestEvidence | None = None,
    ) -> None:
        self.raw = raw
        self.binding = binding
        self.evidence = evidence or PullRequestEvidence(
            repository=binding.repository,
            number=17,
            head_sha="c" * 40,
            state="closed",
            merged=True,
            merge_commit_sha=binding.commit_sha,
            reviews=(_review("c" * 40),),
            checks=(_check("planning-backlog-validation", "c" * 40),),
        )
        self.binding_reads = 0
        self.evidence_reads = 0

    async def pull_request_evidence(
        self,
        repository: str,
        number: int,
        *,
        expected_head_sha: str,
    ) -> PullRequestEvidence:
        self.evidence_reads += 1
        assert (repository, number, expected_head_sha) == (
            self.evidence.repository,
            self.evidence.number,
            self.evidence.head_sha,
        )
        return self.evidence

    async def artifact_binding(
        self,
        *,
        repository: str,
        commit_sha: str,
        path: str,
    ) -> ImmutableArtifactBinding:
        self.binding_reads += 1
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
    run = replace(
        run,
        start_request_ciphertext=_sealed_ordinary_start_request(artifact.plan, run),
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
    github = PublicationGitHubFake(raw, binding)
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
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
    assert github.evidence_reads == 1

    store.run = replace(store.run, approval_evidence_sha256=None)
    openproject.reject_snapshot_reads = True
    outcome = await service.handle(event)
    assert outcome.outcome == "published"
    assert outcome.operation_count == 2
    assert outcome.applied_operation_count == 0
    assert outcome.resumed is True
    assert store.run is not None and store.run.state == "published"
    assert openproject.effects == ["create_work_package", "record_audit"]
    assert github.evidence_reads == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence",
    (
        PullRequestEvidence(
            repository="Acme/service",
            number=17,
            head_sha="c" * 40,
            state="closed",
            merged=True,
            merge_commit_sha="a" * 40,
            reviews=(),
            checks=(_check("planning-backlog-validation", "c" * 40),),
        ),
        PullRequestEvidence(
            repository="Acme/service",
            number=17,
            head_sha="c" * 40,
            state="closed",
            merged=True,
            merge_commit_sha="a" * 40,
            reviews=(_review("c" * 40),),
            checks=(),
        ),
        PullRequestEvidence(
            repository="Acme/service",
            number=17,
            head_sha="c" * 40,
            state="closed",
            merged=True,
            merge_commit_sha="a" * 40,
            reviews=(_review("c" * 40),),
            checks=(
                _check(
                    "planning-backlog-validation",
                    "c" * 40,
                    conclusion="failure",
                ),
            ),
        ),
        PullRequestEvidence(
            repository="Acme/service",
            number=17,
            head_sha="c" * 40,
            state="closed",
            merged=True,
            merge_commit_sha="d" * 40,
            reviews=(_review("c" * 40),),
            checks=(_check("planning-backlog-validation", "c" * 40),),
        ),
    ),
)
async def test_planning_publication_fails_closed_without_exact_approval_evidence(
    evidence: PullRequestEvidence,
) -> None:
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
    store = MemoryStore(run)
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
    github = PublicationGitHubFake(raw, binding, evidence=evidence)
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )
    now = datetime(2026, 7, 30, 20, 0, tzinfo=UTC)
    event = envelope_for_delivery(
        event_type="github.pull_request",
        source="github",
        delivery_id=f"invalid-approval-{hash(evidence)}",
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

    with pytest.raises(LifecycleEventRejected):
        await service.handle(event)

    assert store.run is not None and store.run.state == "pr_open"
    assert github.binding_reads == 0
    assert openproject.effects == []


class InvalidArtifactPlanner:
    def __init__(self, bundle: ArtifactBundle) -> None:
        self.bundle = bundle

    async def artifacts(self, thread_id: str) -> ArtifactBundle:
        assert thread_id == self.bundle.thread_id
        return self.bundle


class NoPlanningCommitGitHubFake:
    async def ensure_planning_commit(self, **_values: object) -> None:
        raise AssertionError("invalid planner artifacts reached a GitHub side effect")


@pytest.mark.asyncio
async def test_semantically_invalid_generated_backlog_never_reaches_github() -> None:
    fixture = Path(__file__).parents[1] / "evals/fixtures/single-repository/backlog.yaml"
    loaded = load_artifact(fixture)
    invalid_item = loaded.plan.items[0].model_copy(
        update={
            "acceptance_criteria": (
                loaded.plan.items[0]
                .acceptance_criteria[0]
                .model_copy(update={"observation": "As desired"}),
            )
        }
    )
    invalid_plan = loaded.plan.model_copy(update={"items": (invalid_item, *loaded.plan.items[1:])})
    content = yaml.safe_dump(
        invalid_plan.model_dump(mode="json"),
        sort_keys=False,
    )
    digest = hashlib.sha256(content.encode()).hexdigest()
    run = PlanRun(
        idea_id=1,
        plan_id="single-repository",
        plan_version=1,
        thread_id="openproject:1:planning:1",
        repository="Acme/service",
        base_branch="a" * 40,
        artifact_prefix="planning/single-repository/v1",
        backlog_path="planning/single-repository/v1/backlog.yaml",
        snapshot_sha256="b" * 64,
        snapshot_etag="fixture",
        state="planning",
    )
    bundle = ArtifactBundle(
        thread_id=run.thread_id,
        artifacts=(ArtifactContent(path="backlog.yaml", sha256=digest, content=content),),
    )
    response = PlanResponse(
        thread_id=run.thread_id,
        status="artifacts_ready",
        trace_id=str(uuid4()),
        interrupt=None,
        artifact_manifest=(ArtifactManifestEntry(path="backlog.yaml", sha256=digest),),
    )
    service = LifecycleService(
        planner=InvalidArtifactPlanner(bundle),  # type: ignore[arg-type]
        openproject=OpenProjectFake(),  # type: ignore[arg-type]
        github=NoPlanningCommitGitHubFake(),  # type: ignore[arg-type]
        store=MemoryStore(run),  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )
    now = datetime(2026, 7, 30, 20, 0, tzinfo=UTC)
    event = envelope_for_delivery(
        event_type="openproject.work_package_changed",
        source="windmill",
        delivery_id="invalid-generated-backlog",
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="service", id="openproject"),
        subject=EventSubject(idea_id=1),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={"work_package": {"id": 1}},
    )

    with pytest.raises(LifecycleEventRejected, match="semantic validation"):
        await service._handle_planner_response(event, run, response)


class ReconciliationStore:
    def __init__(self, runs: tuple[PlanRun, ...]) -> None:
        self.runs = runs
        self.audit_rows: list[tuple[str, str]] = []

    def all(self) -> tuple[PlanRun, ...]:
        return self.runs

    def stale_thread_count(self, _cutoff: datetime) -> int:
        return 0

    def implementation_associations(self) -> tuple[ImplementationPrAssociation, ...]:
        return ()

    def audit(self, **values: Any) -> None:
        self.audit_rows.append((str(values["action"]), str(values["outcome"])))


class StaleThreadStore(ReconciliationStore):
    def __init__(self, runs: tuple[PlanRun, ...], stale_threads: int) -> None:
        super().__init__(runs)
        self.stale_threads = stale_threads
        self.cutoffs: list[datetime] = []

    def stale_thread_count(self, cutoff: datetime) -> int:
        self.cutoffs.append(cutoff)
        return self.stale_threads


class ConvergenceOpenProjectFake:
    def __init__(self, snapshot: OpenProjectSnapshot) -> None:
        self._snapshot = snapshot
        self.snapshot_reads = 0

    def snapshot(self) -> OpenProjectSnapshot:
        self.snapshot_reads += 1
        return self._snapshot


def _published_convergence_run(artifact: object) -> PlanRun:
    return PlanRun(
        idea_id=1,
        plan_id="single-repository",
        plan_version=1,
        thread_id="openproject:1:planning:1",
        repository="Acme/service",
        base_branch="a" * 40,
        artifact_prefix="planning/single-repository/v1",
        backlog_path="planning/single-repository/v1/backlog.yaml",
        snapshot_sha256="b" * 64,
        snapshot_etag="fixture",
        state="published",
        planning_commit="c" * 40,
        pull_request_number=17,
        pull_request_url="https://github.com/Acme/service/pull/17",
        approved_commit="a" * 40,
        approval_evidence_sha256="d" * 64,
        backlog_blob_sha1=artifact.blob_sha1,  # type: ignore[attr-defined]
        backlog_sha256=artifact.sha256,  # type: ignore[attr-defined]
    )


def _convergence_event(*, plan_id: str = "single-repository", plan_version: int = 1):
    now = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
    return envelope_for_delivery(
        event_type="planning.convergence_check",
        source="windmill",
        delivery_id="convergence:wm-job-1176",
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="system", id="windmill-operator"),
        subject=EventSubject(plan_id=plan_id, plan_version=plan_version),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={"plan_id": plan_id, "plan_version": plan_version},
    )


@pytest.mark.asyncio
async def test_convergence_proof_audits_zero_operations_without_mutating() -> None:
    fixture = Path(__file__).parents[1] / "evals/fixtures/single-repository/backlog.yaml"
    artifact = load_artifact(fixture)
    run = _published_convergence_run(artifact)
    approved = with_approved_commit(artifact.plan, run.approved_commit)
    item = approved.items[0]
    snapshot = OpenProjectSnapshot(
        captured_at="2026-07-31T03:00:00Z",
        etag="fixture",
        sha256="b" * 64,
        work_packages=(
            WorkPackageSnapshot(
                id=101,
                lock_version=1,
                plan_id=run.plan_id,
                node_key=item.key,
                plan_version=run.plan_version,
                repository=item.repository,
                managed_hash=managed_hash(approved, item),
            ),
        ),
    )
    store = MemoryStore(run)
    binding = ImmutableArtifactBinding(
        repository=run.repository,
        commit_sha=run.approved_commit,
        path=run.backlog_path,
        blob_sha=run.backlog_blob_sha1,
        content_sha256=run.backlog_sha256,
    )
    openproject = ConvergenceOpenProjectFake(snapshot)
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=openproject,  # type: ignore[arg-type]
        github=PublicationGitHubFake(fixture.read_bytes(), binding),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )

    outcome = await service.handle(_convergence_event())

    assert outcome.action == "convergence_check"
    assert outcome.outcome == "zero_operations"
    assert store.audit_rows == [("convergence_check", "zero_operations")]
    assert openproject.snapshot_reads == 1
    assert store.run == run


@pytest.mark.asyncio
async def test_convergence_proof_reports_drift_and_rejects_unpublished_or_mismatched_identity() -> (
    None
):
    fixture = Path(__file__).parents[1] / "evals/fixtures/single-repository/backlog.yaml"
    artifact = load_artifact(fixture)
    run = _published_convergence_run(artifact)
    binding = ImmutableArtifactBinding(
        repository=run.repository,
        commit_sha=run.approved_commit,
        path=run.backlog_path,
        blob_sha=run.backlog_blob_sha1,
        content_sha256=run.backlog_sha256,
    )
    store = MemoryStore(run)
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=ConvergenceOpenProjectFake(
            OpenProjectSnapshot("2026-07-31T03:00:00Z", "fixture", "b" * 64, ())
        ),  # type: ignore[arg-type]
        github=PublicationGitHubFake(fixture.read_bytes(), binding),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        **_service_policy(),
    )

    assert (await service.handle(_convergence_event())).outcome == "drift_operations:1"
    with pytest.raises(LifecycleEventRejected, match="exact plan identity"):
        await service.handle(
            _convergence_event(plan_id="single-repository").model_copy(
                update={"payload": {"plan_id": "other", "plan_version": 1}}
            )
        )
    store.run = replace(run, state="publishing")
    with pytest.raises(LifecycleEventRejected, match="published"):
        await service.handle(_convergence_event())


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
async def test_reconciliation_reports_stale_threads_without_repairing_them() -> None:
    now = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
    store = StaleThreadStore((), stale_threads=2)
    service = LifecycleService(
        planner=object(),  # type: ignore[arg-type]
        openproject=ReconciliationOpenProjectFake(
            OpenProjectSnapshot("2026-07-31T03:00:00Z", "snapshot", "b" * 64, ())
        ),  # type: ignore[arg-type]
        github=ReconciliationGitHubFake({}),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        publication_database_url="postgresql://unused",
        recovery_cipher=_cipher(),
        planning_thread_stale_after=timedelta(seconds=90),
        **_service_policy(),
    )
    event = envelope_for_delivery(
        event_type="reconciliation.scheduled",
        source="scheduler",
        delivery_id="stale-thread-finding",
        occurred_at=now,
        received_at=now,
        actor=EventActor(kind="system", id="windmill"),
        subject=EventSubject(),
        signature=VerifiedSignature(verified=True, algorithm="internal"),
        payload={"schedule": "nightly"},
    )

    outcome = await service.handle(event)

    assert "stale_threads:2" in outcome.outcome
    assert store.cutoffs == [now - timedelta(seconds=90)]
    assert store.audit_rows[-1] == ("nightly_reconciliation", outcome.outcome)


@pytest.mark.asyncio
async def test_reconciliation_uses_latest_published_graph_during_unapproved_replan() -> None:
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
    current_blocker = base.model_copy(update={"key": "current-blocker", "title": "Current blocker"})
    current_target = base.model_copy(
        update={
            "key": "current-target",
            "title": "Current target",
            "blocked_by": ("current-blocker",),
        }
    )
    version_one = loaded.plan.model_copy(update={"items": (legacy_blocker, legacy_target)})
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
            backlog_blob_sha1=hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest(),
            backlog_sha256=hashlib.sha256(raw).hexdigest(),
        )

    pending_version_three = replace(
        published_run(2, commit_two, raw_two),
        plan_version=3,
        thread_id="openproject:1:planning:3",
        artifact_prefix="planning/single-repository/v3",
        backlog_path="planning/single-repository/v3/backlog.yaml",
        state="pr_open",
        approved_commit=None,
        backlog_blob_sha1=None,
        backlog_sha256=None,
    )
    runs = (
        published_run(1, commit_one, raw_one),
        published_run(2, commit_two, raw_two),
        pending_version_three,
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
        **_service_policy(),
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
