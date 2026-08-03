"""Deterministic Windmill lifecycle coordinator over typed service adapters."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from planning_platform.diff import plan_diff
from planning_platform.github_adapter import GitHubAdapter, GitHubAdapterError
from planning_platform.github_models import ImmutableArtifactBinding, PullRequestEvidence
from planning_platform.lifecycle.models import (
    EventActor,
    EventEnvelope,
    EventSubject,
    VerifiedSignature,
    envelope_for_delivery,
)
from planning_platform.loader import LoadedArtifact, load_artifact_bytes
from planning_platform.models import BacklogPlan, with_approved_commit
from planning_platform.openproject import managed_hash
from planning_platform.openproject_adapter import (
    OpenProjectConflict,
    OpenProjectPublicationAdapter,
    OpenProjectPublicationError,
)
from planning_platform.planner.models import (
    AbandonTerminalResumeRequest,
    IdeaSnapshot,
    OpenProjectSnapshotInput,
    PlannerEvent,
    PlanResponse,
    ReplanContext,
    ResumePlanRequest,
    StartPlanRequest,
    idea_snapshot_digest,
)
from planning_platform.publication_journal import (
    PostgresPublicationJournal,
    PublicationJournal,
    PublicationJournalMismatch,
)
from planning_platform.publisher import (
    PublicationEnvelope,
    PublicationRejected,
    ReplanPublicationContext,
    publish,
)
from planning_platform.replan import effective_node_binding, validate_replan_candidate
from planning_platform.validation import validate_plan

from .concurrency import run_sync_to_completion
from .planner_client import PlannerClient, PlannerThreadNotFound
from .recovery import RecoveryCipher, RecoveryPayloadRejected
from .store import (
    ImplementationPrAssociation,
    LifecycleStoreMismatch,
    PlanRun,
    PostgresLifecycleStore,
)

_REPOSITORY_ENTRY = re.compile(
    r"(?:https://github\.com/)?"
    r"(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]*[A-Za-z0-9_-])"
    r"(?:\.git)?/?"
)
_RELEVANT_REPOSITORIES_HEADING = re.compile(
    r"^[ \t]{0,3}#{1,6}[ \t]+relevant repositories(?:[ \t]+#+)?[ \t]*$",
    re.IGNORECASE,
)
_MARKDOWN_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)")
_MARKDOWN_FENCE = re.compile(
    r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<tail>.*)$"
)
_MARKDOWN_LIST_PREFIX = re.compile(
    r"^(?:(?:[-+*]|[0-9]{1,9}[.)])[ \t]+)"
)
_MARKDOWN_CODE_SPAN = re.compile(
    r"^(?P<fence>`+)(?P<value>.+)(?P=fence)$"
)
_MARKDOWN_LINK = re.compile(
    r"^\[[^\]\r\n]+\]\([ \t]*"
    r"(?:<(?P<angle>https://github\.com/[^>\s]+)>|"
    r"(?P<plain>https://github\.com/[^\s)]+))"
    r"(?:[ \t]+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\([^()\r\n]*\)))?"
    r"[ \t]*\)$"
)
_REQUIRED_PLANNING_CHECKS = frozenset({"planning-backlog-validation"})


def _without_html_comments(
    line: str,
    *,
    in_comment: bool,
) -> tuple[str | None, bool]:
    contains_comment = in_comment
    cursor = 0
    while cursor < len(line):
        if in_comment:
            end = line.find("-->", cursor)
            if end == -1:
                return None, True
            cursor = end + 3
            in_comment = False
            continue
        start = line.find("<!--", cursor)
        if start == -1:
            break
        contains_comment = True
        cursor = start + 4
        in_comment = True
    return (None if contains_comment else line), in_comment


def _visible_markdown_lines(description: str) -> tuple[str | None, ...]:
    visible: list[str | None] = []
    fence_character: str | None = None
    fence_length = 0
    in_html_comment = False
    for line in description.splitlines():
        if fence_character is not None:
            fence = _MARKDOWN_FENCE.match(line)
            if fence is not None:
                marker = fence.group("fence")
                if (
                    marker[0] == fence_character
                    and len(marker) >= fence_length
                    and not fence.group("tail").strip()
                ):
                    fence_character = None
                    fence_length = 0
            continue
        if not in_html_comment:
            fence = _MARKDOWN_FENCE.match(line)
            if fence is not None:
                marker = fence.group("fence")
                fence_character = marker[0]
                fence_length = len(marker)
                continue
        visible_line, in_html_comment = _without_html_comments(
            line,
            in_comment=in_html_comment,
        )
        visible.append(visible_line)
    return tuple(visible)


def _markdown_section_boundary(line: str | None) -> bool:
    return line is None or _MARKDOWN_HEADING.match(line) is not None


def _repository_entry(line: str) -> str | None:
    if line.startswith("\t") or line.startswith("    "):
        return None
    candidate = _MARKDOWN_LIST_PREFIX.sub("", line.strip(), count=1).strip()
    if not candidate:
        return None
    code_span = _MARKDOWN_CODE_SPAN.fullmatch(candidate)
    if code_span is not None:
        candidate = code_span.group("value").strip()
    elif candidate.startswith("<") and candidate.endswith(">"):
        candidate = candidate[1:-1].strip()
    else:
        link = _MARKDOWN_LINK.fullmatch(candidate)
        if link is not None:
            candidate = link.group("angle") or link.group("plain")
    match = _REPOSITORY_ENTRY.fullmatch(candidate)
    if match is None:
        return None
    return match.group("repository").removesuffix(".git")


@dataclass(frozen=True)
class LifecycleOutcome:
    action: str
    outcome: str
    plan_id: str | None = None
    plan_version: int | None = None
    work_package_id: int | None = None
    pull_request_number: int | None = None
    operation_count: int | None = None
    applied_operation_count: int | None = None
    resumed: bool | None = None


class LifecycleEventRejected(ValueError):
    """A verified delivery does not satisfy lifecycle state preconditions."""


class LifecycleService:
    """Coordinates effects; the planner itself is never given mutation clients."""

    def __init__(
        self,
        *,
        planner: PlannerClient,
        openproject: OpenProjectPublicationAdapter,
        github: GitHubAdapter,
        store: PostgresLifecycleStore,
        publication_database_url: str,
        recovery_cipher: RecoveryCipher,
        planning_repository: str,
        implementation_required_checks: Mapping[str, Collection[str]],
        implementation_stale_after: timedelta = timedelta(hours=48),
        planning_thread_stale_after: timedelta = timedelta(hours=24),
        publication_journal_factory: Callable[[], PublicationJournal] | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", planning_repository):
            raise ValueError("planning artifact repository is invalid")
        if implementation_stale_after <= timedelta(0):
            raise ValueError("implementation stale interval must be positive")
        if planning_thread_stale_after <= timedelta(0):
            raise ValueError("planning thread stale interval must be positive")
        normalized_checks: dict[str, frozenset[str]] = {}
        for repository, checks in implementation_required_checks.items():
            values = frozenset(checks)
            if (
                not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or not values
                or any(not value.strip() for value in values)
            ):
                raise ValueError("implementation required-check policy is invalid")
            normalized_checks[repository] = values
        self._planner = planner
        self._openproject = openproject
        self._github = github
        self._store = store
        self._publication_database_url = publication_database_url
        self._recovery_cipher = recovery_cipher
        self._planning_repository = planning_repository
        self._implementation_required_checks = normalized_checks
        self._implementation_stale_after = implementation_stale_after
        self._planning_thread_stale_after = planning_thread_stale_after
        self._publication_journal_factory = publication_journal_factory or (
            lambda: PostgresPublicationJournal(self._publication_database_url)
        )

    async def abandon_terminal_resume(
        self,
        *,
        plan_id: str,
        plan_version: int,
        thread_id: str,
        idempotency_key: str,
        operator: str,
        reason: str,
    ) -> LifecycleOutcome:
        if not operator.strip() or len(operator) > 255:
            raise LifecycleEventRejected("terminal resume abandonment operator is required")
        if not reason.strip() or len(reason) > 1_024:
            raise LifecycleEventRejected("terminal resume abandonment reason is required")
        run = await run_sync_to_completion(self._store.get, plan_id, plan_version)
        if run is None or run.thread_id != thread_id or run.state != "needs_input":
            raise LifecycleEventRejected(
                "terminal resume abandonment does not match a Needs Input lifecycle run"
            )
        delivery = await run_sync_to_completion(
            self._store.terminal_delivery,
            idempotency_key,
        )
        if (
            delivery is None
            or delivery.state != "dead_letter"
            or delivery.lease_expires_at is not None
            or delivery.claim_token is not None
        ):
            raise LifecycleEventRejected("resume delivery is not terminal and unfenced")
        if run.pending_resume_ciphertext is None:
            completed = await run_sync_to_completion(
                self._store.completed_terminal_resume_abandonment,
                plan_id=run.plan_id,
                plan_version=run.plan_version,
                thread_id=run.thread_id,
                idempotency_key=idempotency_key,
                operator=operator.strip(),
                reason=reason.strip(),
            )
            if completed is None:
                raise LifecycleEventRejected("lifecycle run has no pending resume to abandon")
            response = await self._planner.get(run.thread_id)
            if (
                response.status != "needs_input"
                or response.interrupt is None
                or response.interrupt.interrupt_id != completed.interrupt_id
                or response.artifact_manifest
            ):
                raise LifecycleEventRejected(
                    "completed terminal resume abandonment no longer matches planner state"
                )
            return LifecycleOutcome(
                action="terminal_resume_abandonment",
                outcome="restored_interrupt",
                plan_id=run.plan_id,
                plan_version=run.plan_version,
                work_package_id=run.idea_id,
            )
        try:
            plaintext = self._recovery_cipher.open(
                purpose="planner-resume",
                binding=run.thread_id,
                ciphertext=run.pending_resume_ciphertext,
            )
            pending = ResumePlanRequest.model_validate_json(plaintext)
        except (RecoveryPayloadRejected, ValueError) as error:
            raise LifecycleEventRejected(
                "durable lifecycle run has an invalid pending resume"
            ) from error
        if pending.event.idempotency_key != idempotency_key:
            raise LifecycleEventRejected(
                "terminal delivery does not own the pending planner resume"
            )

        response = await self._planner.abandon_terminal_resume(
            run.thread_id,
            AbandonTerminalResumeRequest(
                idempotency_key=idempotency_key,
                interrupt_id=pending.interrupt_id,
                comment_id=pending.comment_id,
                comment_created_at=pending.comment_created_at,
                operator=operator.strip(),
                reason=reason.strip(),
            ),
        )
        if (
            response.thread_id != run.thread_id
            or response.status != "needs_input"
            or response.interrupt is None
            or response.interrupt.interrupt_id != pending.interrupt_id
            or response.artifact_manifest
        ):
            raise LifecycleEventRejected(
                "planner did not restore the exact artifact-free interrupt"
            )
        await run_sync_to_completion(
            self._store.complete_terminal_resume_abandonment,
            run=run,
            request_ciphertext=run.pending_resume_ciphertext,
            idempotency_key=idempotency_key,
            trace_id=str(pending.event.trace_id),
            interrupt_id=pending.interrupt_id,
            operator=operator.strip(),
            reason=reason.strip(),
        )
        return LifecycleOutcome(
            action="terminal_resume_abandonment",
            outcome="restored_interrupt",
            plan_id=run.plan_id,
            plan_version=run.plan_version,
            work_package_id=run.idea_id,
        )

    async def handle(self, event: EventEnvelope) -> LifecycleOutcome:
        if not event.signature.verified:
            raise LifecycleEventRejected("lifecycle service requires a verified event")
        if event.event_type == "openproject.work_package_changed":
            return await self._work_package_changed(event)
        if event.event_type == "openproject.idea_comment":
            return await self._planning_input(event)
        if event.event_type == "github.pull_request":
            return await self._pull_request(event)
        if event.event_type == "github.check_run":
            return await self._check_run(event)
        if event.event_type == "planning.convergence_check":
            return await self._convergence_check(event)
        if event.event_type == "planning.replan_affected_subgraph":
            return await self._replan_affected_subgraph(event)
        if event.event_type == "reconciliation.scheduled":
            return await self._reconcile(event)
        raise LifecycleEventRejected(f"unsupported lifecycle event: {event.event_type}")

    async def _convergence_check(self, event: EventEnvelope) -> LifecycleOutcome:
        """Prove an approved immutable plan is converged without mutating anything."""
        if event.source != "windmill" or event.signature.algorithm != "internal":
            raise LifecycleEventRejected("convergence proof requires a Windmill internal event")
        plan_id = event.subject.plan_id
        plan_version = event.subject.plan_version
        if (
            not plan_id
            or plan_version is None
            or plan_version <= 0
            or event.payload != {"plan_id": plan_id, "plan_version": plan_version}
        ):
            raise LifecycleEventRejected("convergence proof requires one exact plan identity")
        run = await run_sync_to_completion(self._store.get, plan_id, plan_version)
        if run is None:
            raise LifecycleEventRejected("convergence proof references an unknown plan version")
        if run.state != "published":
            raise LifecycleEventRejected("convergence proof requires a published plan version")
        if (
            run.approved_commit is None
            or run.approval_evidence_sha256 is None
            or run.backlog_blob_sha1 is None
            or run.backlog_sha256 is None
        ):
            raise LifecycleEventRejected("published plan is missing immutable publication bindings")
        binding = ImmutableArtifactBinding(
            repository=run.repository,
            commit_sha=run.approved_commit,
            path=run.backlog_path,
            blob_sha=run.backlog_blob_sha1,
            content_sha256=run.backlog_sha256,
        )
        artifact = load_artifact_bytes(await self._github.read_immutable_artifact(binding))
        if (
            artifact.plan.plan.id != plan_id
            or artifact.plan.plan.version != plan_version
            or artifact.blob_sha1 != binding.blob_sha
            or artifact.sha256 != binding.content_sha256
        ):
            raise LifecycleEventRejected(
                "immutable artifact does not match the published plan identity"
            )
        snapshot = await run_sync_to_completion(self._openproject.snapshot)
        approved = with_approved_commit(artifact.plan, run.approved_commit)
        operations = plan_diff(approved, snapshot, trace_id=str(event.trace_id))
        mutation_count = sum(operation.kind != "record_audit" for operation in operations)
        outcome = "zero_operations" if mutation_count == 0 else f"drift_operations:{mutation_count}"
        return await self._outcome(
            event,
            "convergence_check",
            outcome,
            plan_id=plan_id,
            plan_version=plan_version,
        )

    async def _replan_affected_subgraph(self, event: EventEnvelope) -> LifecycleOutcome:
        """Start exactly one bounded successor of an immutable published plan."""
        if event.source != "windmill" or event.signature.algorithm != "internal":
            raise LifecycleEventRejected("partial replan requires a Windmill internal event")
        plan_id = event.subject.plan_id
        base_version = event.subject.plan_version
        payload = event.payload
        roots = payload.get("affected_node_keys")
        reason = payload.get("reason")
        if (
            not plan_id
            or base_version is None
            or type(base_version) is not int
            or not isinstance(roots, list)
            or not roots
            or any(not isinstance(key, str) or not key for key in roots)
            or len(roots) != len(set(roots))
            or not isinstance(reason, str)
            or not reason.strip()
            or payload
            != {
                "plan_id": plan_id,
                "base_plan_version": base_version,
                "affected_node_keys": roots,
                "reason": reason,
            }
        ):
            raise LifecycleEventRejected("partial replan requires one exact bounded request")
        base = await run_sync_to_completion(self._store.get, plan_id, base_version)
        if base is None:
            raise LifecycleEventRejected("partial replan references an unknown base plan version")
        if base.state != "published":
            raise LifecycleEventRejected("partial replan base version must be published")
        if (
            base.approved_commit is None
            or base.approval_evidence_sha256 is None
            or base.backlog_blob_sha1 is None
            or base.backlog_sha256 is None
        ):
            raise LifecycleEventRejected(
                "partial replan base is missing immutable publication bindings"
            )
        binding = ImmutableArtifactBinding(
            repository=base.repository,
            commit_sha=base.approved_commit,
            path=base.backlog_path,
            blob_sha=base.backlog_blob_sha1,
            content_sha256=base.backlog_sha256,
        )
        artifact = load_artifact_bytes(await self._github.read_immutable_artifact(binding))
        prior = artifact.plan
        if (
            prior.plan.id != base.plan_id
            or prior.plan.version != base.plan_version
            or prior.plan.source_idea.work_package_id != base.idea_id
            or artifact.blob_sha1 != binding.blob_sha
            or artifact.sha256 != binding.content_sha256
        ):
            raise LifecycleEventRejected("immutable base backlog does not match its published run")
        missing = set(roots) - set(prior.by_key)
        if missing:
            raise LifecycleEventRejected("partial replan references unknown affected roots")
        selected = tuple(roots)
        expected_replan = ReplanContext(
            prior_plan=prior,
            base_approved_planning_commit=base.approved_commit,
            selected_root_keys=selected,
            affected_node_keys=self._prior_descendant_keys(prior, set(selected)),
            reason=reason,
        )
        published = await run_sync_to_completion(self._store.latest_published, plan_id)
        if published != base:
            raise LifecycleEventRejected(
                "partial replan base version is not the latest published plan"
            )
        latest = await run_sync_to_completion(self._store.latest_for_idea, base.idea_id)
        plan_version = base.plan_version + 1
        if latest != base:
            if (
                latest is None
                or latest.plan_id != base.plan_id
                or latest.idea_id != base.idea_id
                or latest.plan_version <= base.plan_version
            ):
                raise LifecycleEventRejected(
                    "partial replan has an invalid successor history"
                )
            previous = self._recover_replan_request(latest)
            expected_event = PlannerEvent(
                idempotency_key=event.idempotency_key,
                trace_id=event.trace_id,
            )
            if previous.event == expected_event and previous.replan == expected_replan:
                return await self._resume_existing_replan(
                    event,
                    base,
                    latest,
                    expected_replan,
                )
            previous_replan = previous.replan
            if (
                latest.state != "failed"
                or previous_replan is None
                or previous.plan_id != base.plan_id
                or previous.plan_version != latest.plan_version
                or previous.idea.work_package_id != base.idea_id
                or previous_replan.prior_plan != prior
                or previous_replan.base_approved_planning_commit != base.approved_commit
            ):
                raise LifecycleEventRejected(
                    "partial replan already has a different active successor"
                )
            plan_version = latest.plan_version + 1
        context = []
        current = await run_sync_to_completion(self._openproject.read_work_package, base.idea_id)
        description = self._description(current)
        repositories = self._repositories(description)
        if not repositories:
            raise LifecycleEventRejected("partial replan Idea no longer names a repository")
        for repository in repositories:
            _branch, snapshot = await self._github.context_snapshot(repository)
            context.append(snapshot)
        base_branch, _planning_head = await self._github.repository_head(self._planning_repository)
        op_snapshot = await run_sync_to_completion(self._openproject.snapshot)
        thread_id = f"openproject:{base.idea_id}:planning:{plan_version}"
        artifact_prefix = f"planning/{base.plan_id}/v{plan_version}"
        run = PlanRun(
            idea_id=base.idea_id,
            plan_id=base.plan_id,
            plan_version=plan_version,
            thread_id=thread_id,
            repository=self._planning_repository,
            base_branch=base_branch,
            artifact_prefix=artifact_prefix,
            backlog_path=f"{artifact_prefix}/backlog.yaml",
            snapshot_sha256=op_snapshot.sha256,
            snapshot_etag=op_snapshot.etag,
            state="planning",
        )
        idea = IdeaSnapshot(
            work_package_id=base.idea_id,
            lock_version=self._nonnegative_int(current.get("lockVersion"), "lockVersion"),
            updated_at=self._timestamp(current.get("updatedAt"), "updatedAt"),
            title=str(current.get("subject", "")).strip(),
            description=description,
        )
        snapshot_input = OpenProjectSnapshotInput(
            captured_at=self._timestamp(op_snapshot.captured_at, "snapshot captured_at"),
            etag=op_snapshot.etag,
            sha256=op_snapshot.sha256,
        )
        request = StartPlanRequest(
            event=PlannerEvent(idempotency_key=event.idempotency_key, trace_id=event.trace_id),
            idea=idea,
            plan_id=base.plan_id,
            plan_version=plan_version,
            idea_sha256=idea_snapshot_digest(idea, snapshot_input),
            openproject_snapshot=snapshot_input,
            repositories=tuple(context),
            replan=expected_replan,
        )
        run = replace(
            run,
            start_request_ciphertext=self._recovery_cipher.seal(
                purpose="planner-start", binding=run.thread_id, plaintext=request.model_dump_json()
            ),
        )
        run = await run_sync_to_completion(self._store.begin, run)
        await run_sync_to_completion(
            self._openproject.set_lifecycle_state, base.idea_id, status="Planning"
        )
        response = await self._planner.start(request)
        return await self._handle_planner_response(event, run, response)

    async def _resume_existing_replan(
        self,
        event: EventEnvelope,
        base: PlanRun,
        run: PlanRun,
        expected_replan: ReplanContext,
    ) -> LifecycleOutcome:
        """Recover only the exact operator request that inserted this successor."""
        request = self._recover_replan_request(run)
        expected_event = PlannerEvent(
            idempotency_key=event.idempotency_key,
            trace_id=event.trace_id,
        )
        if (
            run.idea_id != base.idea_id
            or run.plan_id != base.plan_id
            or run.plan_version <= base.plan_version
            or run.thread_id != f"openproject:{base.idea_id}:planning:{run.plan_version}"
            or request.event != expected_event
            or request.idea.work_package_id != base.idea_id
            or request.plan_id != base.plan_id
            or request.plan_version != run.plan_version
            or request.openproject_snapshot.sha256 != run.snapshot_sha256
            or request.openproject_snapshot.etag != run.snapshot_etag
            or request.replan != expected_replan
        ):
            raise LifecycleEventRejected(
                "bounded replan replay changed its immutable successor binding"
            )
        if run.state == "published":
            return await self._outcome(
                event,
                "replan_affected_subgraph",
                "already_published",
                plan_id=run.plan_id,
                plan_version=run.plan_version,
                work_package_id=run.idea_id,
                pull_request_number=run.pull_request_number,
            )
        if run.state == "failed":
            return await self._outcome(
                event,
                "replan_affected_subgraph",
                "already_failed",
                plan_id=run.plan_id,
                plan_version=run.plan_version,
                work_package_id=run.idea_id,
            )
        if run.state not in {"planning", "needs_input", "pr_open"}:
            raise LifecycleEventRejected(
                "bounded replan successor is already in a publication transition"
            )
        await run_sync_to_completion(
            self._openproject.set_lifecycle_state,
            run.idea_id,
            status="Planning",
        )
        try:
            response = await self._planner.get(run.thread_id)
        except PlannerThreadNotFound:
            response = await self._planner.start(request)
        return await self._handle_planner_response(event, run, response)

    def _recover_start_request(self, run: PlanRun) -> StartPlanRequest:
        try:
            plaintext = self._recovery_cipher.open(
                purpose="planner-start",
                binding=run.thread_id,
                ciphertext=run.start_request_ciphertext,
            )
            request = StartPlanRequest.model_validate_json(plaintext)
        except (RecoveryPayloadRejected, ValueError) as error:
            raise LifecycleEventRejected(
                "lifecycle run has no recoverable planner start request"
            ) from error
        return request

    def _recover_replan_request(self, run: PlanRun) -> StartPlanRequest:
        request = self._recover_start_request(run)
        if request.replan is None:
            raise LifecycleEventRejected(
                "bounded replan successor has no partial-replan binding"
            )
        return request

    async def _work_package_changed(self, event: EventEnvelope) -> LifecycleOutcome:
        raw = self._payload_object(event.payload, "work_package")
        idea_id = self._positive_int(raw.get("id"), "work-package ID")
        if self._link_title(raw, "type").casefold() != "idea":
            return await self._outcome(event, "ignore_non_idea", "ignored", work_package_id=idea_id)
        status = self._link_title(raw, "status")
        if status.casefold() != "planning":
            return await self._outcome(
                event, "ignore_non_planning", "ignored", work_package_id=idea_id
            )

        latest = await run_sync_to_completion(self._store.latest_for_idea, idea_id)
        if (
            event.actor.kind == "service"
            and latest is not None
            and latest.state not in {"published", "failed"}
        ):
            start_request = self._recover_start_request(latest)
            if start_request.event.idempotency_key != event.idempotency_key:
                return await self._outcome(
                    event,
                    "ignore_service_status_echo",
                    "ignored_active_run",
                    plan_id=latest.plan_id,
                    plan_version=latest.plan_version,
                    work_package_id=idea_id,
                )
        latest_published = (
            None
            if latest is None
            else await run_sync_to_completion(self._store.latest_published, latest.plan_id)
        )
        if (
            latest is not None
            and latest_published is not None
            and (latest.state == "published" or latest_published.plan_version < latest.plan_version)
        ):
            return await self._outcome(
                event,
                "bounded_replan_required",
                f"ignored_after_{latest.state}",
                plan_id=latest.plan_id,
                plan_version=latest.plan_version,
                work_package_id=idea_id,
            )
        if latest is not None and latest.state not in {"published", "failed"}:
            try:
                response = await self._planner.get(latest.thread_id)
            except PlannerThreadNotFound:
                request = self._recover_start_request(latest)
                response = await self._planner.start(request)
            return await self._handle_planner_response(event, latest, response)

        current = await run_sync_to_completion(self._openproject.read_work_package, idea_id)
        description = self._description(current)
        repositories = self._repositories(description)
        if not repositories:
            await run_sync_to_completion(
                self._openproject.ensure_comment,
                idea_id,
                (
                    "Planning cannot start because **Relevant repositories** does not "
                    "contain an `owner/repository` value."
                ),
                idempotency_key=f"{event.idempotency_key}:missing-repository",
            )
            await run_sync_to_completion(
                self._openproject.set_lifecycle_state,
                idea_id,
                status="Needs Input",
            )
            return await self._outcome(
                event,
                "start_planning",
                "needs_repository",
                work_package_id=idea_id,
            )

        context = []
        for repository in repositories:
            _branch, snapshot = await self._github.context_snapshot(repository)
            context.append(snapshot)
        base_branch, _planning_head = await self._github.repository_head(self._planning_repository)
        op_snapshot = await run_sync_to_completion(self._openproject.snapshot)
        plan_version = 1 if latest is None else latest.plan_version + 1
        plan_id = f"idea-{idea_id}"
        thread_id = f"openproject:{idea_id}:planning:{plan_version}"
        artifact_prefix = f"planning/{plan_id}/v{plan_version}"
        run = PlanRun(
            idea_id=idea_id,
            plan_id=plan_id,
            plan_version=plan_version,
            thread_id=thread_id,
            repository=self._planning_repository,
            base_branch=base_branch,
            artifact_prefix=artifact_prefix,
            backlog_path=f"{artifact_prefix}/backlog.yaml",
            snapshot_sha256=op_snapshot.sha256,
            snapshot_etag=op_snapshot.etag,
            state="planning",
        )
        idea = IdeaSnapshot(
            work_package_id=idea_id,
            lock_version=self._nonnegative_int(current.get("lockVersion"), "lockVersion"),
            updated_at=self._timestamp(current.get("updatedAt"), "updatedAt"),
            title=str(current.get("subject", "")).strip(),
            description=description,
        )
        snapshot_input = OpenProjectSnapshotInput(
            captured_at=self._timestamp(op_snapshot.captured_at, "snapshot captured_at"),
            etag=op_snapshot.etag,
            sha256=op_snapshot.sha256,
        )
        request = StartPlanRequest(
            event=PlannerEvent(
                idempotency_key=event.idempotency_key,
                trace_id=event.trace_id,
            ),
            idea=idea,
            plan_id=plan_id,
            plan_version=plan_version,
            idea_sha256=idea_snapshot_digest(idea, snapshot_input),
            openproject_snapshot=snapshot_input,
            repositories=tuple(context),
        )
        run = replace(
            run,
            start_request_ciphertext=self._recovery_cipher.seal(
                purpose="planner-start",
                binding=run.thread_id,
                plaintext=request.model_dump_json(),
            ),
        )
        run = await run_sync_to_completion(self._store.begin, run)
        response = await self._planner.start(request)
        return await self._handle_planner_response(event, run, response)

    async def _planning_input(self, event: EventEnvelope) -> LifecycleOutcome:
        activity = self._payload_object(event.payload, "activity")
        links = activity.get("_links")
        work_package_link = links.get("workPackage") if isinstance(links, dict) else None
        href = work_package_link.get("href") if isinstance(work_package_link, dict) else None
        if not isinstance(href, str) or not href.rstrip("/").rsplit("/", 1)[-1].isdigit():
            raise LifecycleEventRejected("OpenProject comment has no work-package link")
        idea_id = int(href.rstrip("/").rsplit("/", 1)[-1])
        run = await run_sync_to_completion(self._store.latest_for_idea, idea_id)
        if run is None or run.state != "needs_input":
            return await self._outcome(
                event,
                "resume_planning",
                "no_pending_interrupt",
                work_package_id=idea_id,
            )
        comment = activity.get("comment")
        answer = comment.get("raw") if isinstance(comment, dict) else None
        if event.actor.kind != "human":
            return await self._outcome(
                event,
                "resume_planning",
                "ignored_service_comment",
                plan_id=run.plan_id,
                plan_version=run.plan_version,
                work_package_id=idea_id,
            )
        if run.pending_resume_ciphertext is not None:
            try:
                plaintext = self._recovery_cipher.open(
                    purpose="planner-resume",
                    binding=run.thread_id,
                    ciphertext=run.pending_resume_ciphertext,
                )
                pending = ResumePlanRequest.model_validate_json(plaintext)
            except (RecoveryPayloadRejected, ValueError) as error:
                raise LifecycleEventRejected(
                    "durable lifecycle run has an invalid pending resume"
                ) from error
            response = await self._planner.resume(run.thread_id, pending)
            outcome = await self._handle_planner_response(event, run, response)
            await run_sync_to_completion(
                self._store.clear_pending_resume,
                run,
                run.pending_resume_ciphertext,
            )
            return outcome

        state = await self._planner.get(run.thread_id)
        if state.interrupt is None:
            return await self._handle_planner_response(event, run, state)
        if not isinstance(answer, str) or not answer.strip():
            raise LifecycleEventRejected("OpenProject planning comment is empty")
        comment_id = self._positive_int(activity.get("id"), "comment ID")
        created_at = self._timestamp(activity.get("createdAt"), "comment createdAt")
        resume_request = ResumePlanRequest(
            event=PlannerEvent(
                idempotency_key=event.idempotency_key,
                trace_id=event.trace_id,
            ),
            interrupt_id=state.interrupt.interrupt_id,
            comment_id=comment_id,
            comment_created_at=created_at,
            answer=answer,
        )
        request_ciphertext = self._recovery_cipher.seal(
            purpose="planner-resume",
            binding=run.thread_id,
            plaintext=resume_request.model_dump_json(),
        )
        run = await run_sync_to_completion(
            self._store.record_pending_resume,
            run,
            request_ciphertext,
        )
        response = await self._planner.resume(run.thread_id, resume_request)
        await run_sync_to_completion(
            self._openproject.set_lifecycle_state,
            idea_id,
            status="Planning",
        )
        outcome = await self._handle_planner_response(event, run, response)
        await run_sync_to_completion(
            self._store.clear_pending_resume,
            run,
            request_ciphertext,
        )
        return outcome

    async def _handle_planner_response(
        self, event: EventEnvelope, run: PlanRun, response: PlanResponse
    ) -> LifecycleOutcome:
        if response.thread_id != run.thread_id:
            raise LifecycleEventRejected("planner returned a different durable thread")
        if response.status == "needs_input":
            if response.interrupt is None:
                raise LifecycleEventRejected("planner Needs Input result has no interrupt")
            question = "\n".join(f"- {value}" for value in response.interrupt.questions)
            body = (
                "## Planning input required\n\n"
                f"{question}\n\n"
                f"**Why this affects the plan:** {response.interrupt.impact}"
            )
            await run_sync_to_completion(
                self._openproject.ensure_comment,
                run.idea_id,
                body,
                idempotency_key=f"interrupt:{run.thread_id}:{response.interrupt.interrupt_id}",
            )
            await run_sync_to_completion(
                self._openproject.set_lifecycle_state,
                run.idea_id,
                status="Needs Input",
            )
            await run_sync_to_completion(self._store.set_state, run, "needs_input")
            return await self._outcome(
                event,
                "planner_interrupt",
                "needs_input",
                plan_id=run.plan_id,
                plan_version=run.plan_version,
                work_package_id=run.idea_id,
            )
        if response.status == "failed":
            await run_sync_to_completion(
                self._openproject.ensure_comment,
                run.idea_id,
                (
                    "Planning stopped because the durable planner reported a terminal "
                    "failure. Start a new plan version after correcting the input or "
                    "runtime cause."
                ),
                idempotency_key=f"planner-failed:{run.thread_id}",
            )
            await run_sync_to_completion(
                self._openproject.set_lifecycle_state,
                run.idea_id,
                status="Rejected",
                evidence_state="planning_failed",
            )
            failed = await run_sync_to_completion(self._store.set_state, run, "failed")
            return await self._outcome(
                event,
                "planner_terminal",
                "failed",
                plan_id=failed.plan_id,
                plan_version=failed.plan_version,
                work_package_id=failed.idea_id,
            )
        if response.status != "artifacts_ready":
            return await self._outcome(
                event,
                "planner_progress",
                response.status,
                plan_id=run.plan_id,
                plan_version=run.plan_version,
                work_package_id=run.idea_id,
            )
        bundle = await self._planner.artifacts(run.thread_id)
        expected_manifest = {entry.path: entry.sha256 for entry in response.artifact_manifest}
        actual_manifest = {entry.path: entry.sha256 for entry in bundle.artifacts}
        if expected_manifest != actual_manifest or "backlog.yaml" not in actual_manifest:
            raise LifecycleEventRejected("planner artifact bundle does not match its manifest")
        artifacts = {
            f"{run.artifact_prefix}/{entry.path}": entry.content.encode("utf-8")
            for entry in bundle.artifacts
        }
        backlog = load_artifact_bytes(artifacts[run.backlog_path])
        if (
            backlog.plan.plan.id != run.plan_id
            or backlog.plan.plan.version != run.plan_version
            or backlog.plan.plan.source_idea.work_package_id != run.idea_id
            or backlog.plan.plan.approved_planning_commit is not None
        ):
            raise LifecycleEventRejected("planner backlog does not match its lifecycle run")
        issues = validate_plan(backlog.plan)
        if issues:
            codes = ",".join(sorted({issue.code for issue in issues}))
            raise LifecycleEventRejected(f"planner backlog failed semantic validation ({codes})")
        try:
            plaintext = self._recovery_cipher.open(
                purpose="planner-start",
                binding=run.thread_id,
                ciphertext=run.start_request_ciphertext,
            )
            start_request = StartPlanRequest.model_validate_json(plaintext)
        except (RecoveryPayloadRejected, ValueError) as error:
            raise LifecycleEventRejected(
                "planner artifact has no recoverable lifecycle request"
            ) from error
        if start_request.replan is not None:
            replan = start_request.replan
            try:
                validate_replan_candidate(
                    replan.prior_plan,
                    backlog.plan,
                    base_approved_commit=replan.base_approved_planning_commit,
                    selected_root_keys=replan.selected_root_keys,
                    affected_node_keys=replan.affected_node_keys,
                )
            except ValueError as error:
                raise LifecycleEventRejected(
                    f"planner backlog escaped bounded replan scope: {error}"
                ) from error
        branch = await self._github.ensure_planning_commit(
            repository=run.repository,
            branch_name=f"planning/{run.plan_id}-v{run.plan_version}",
            base=run.base_branch,
            artifacts=artifacts,
            message=f"planning: {run.plan_id} v{run.plan_version}",
        )
        graph = next(
            (entry.content for entry in bundle.artifacts if entry.path == "backlog.mmd"),
            "",
        )
        body = (
            f"Approves planning artifacts for `{run.plan_id}:v{run.plan_version}`.\n\n"
            f"Source Idea: OpenProject #{run.idea_id}\n"
            f"Trace: `{event.trace_id}`\n\n"
            "### Dependency graph\n\n"
            f"```mermaid\n{graph.rstrip()}\n```\n\n"
            "<!-- planning-platform:planning-pr -->"
        )
        pull = await self._github.ensure_planning_pull_request(
            branch,
            base=run.base_branch,
            title=f"Planning: {run.plan_id} v{run.plan_version}",
            body=body,
        )
        stored = await run_sync_to_completion(
            self._store.record_pull_request,
            plan_id=run.plan_id,
            plan_version=run.plan_version,
            planning_commit=branch.commit_sha,
            number=pull.number,
            url=pull.url,
        )
        await run_sync_to_completion(
            self._openproject.ensure_comment,
            run.idea_id,
            f"Planning artifacts are ready for approval: {pull.url}",
            idempotency_key=f"planning-pr:{run.plan_id}:v{run.plan_version}",
        )
        return await self._outcome(
            event,
            "prepare_planning_pr",
            "pr_open",
            plan_id=stored.plan_id,
            plan_version=stored.plan_version,
            work_package_id=stored.idea_id,
            pull_request_number=stored.pull_request_number,
        )

    async def _pull_request(self, event: EventEnvelope) -> LifecycleOutcome:
        payload = event.payload
        pull = self._payload_object(payload, "pull_request")
        repository_value = self._payload_object(payload, "repository")
        repository = repository_value.get("full_name")
        if not isinstance(repository, str):
            raise LifecycleEventRejected("GitHub pull request has no repository identity")
        number = self._positive_int(pull.get("number", payload.get("number")), "pull request")
        run = await run_sync_to_completion(self._store.by_pull_request, repository, number)
        if run is None:
            return await self._implementation_pull_request(event, repository, pull)
        if run.state == "published":
            return await self._outcome(
                event,
                "planning_pr",
                "already_published",
                plan_id=run.plan_id,
                plan_version=run.plan_version,
                pull_request_number=number,
            )
        if pull.get("merged") is not True:
            return await self._outcome(
                event,
                "planning_pr",
                "not_merged",
                plan_id=run.plan_id,
                plan_version=run.plan_version,
                pull_request_number=number,
            )
        merge_sha = pull.get("merge_commit_sha")
        if not isinstance(merge_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", merge_sha):
            raise LifecycleEventRejected("merged planning PR has no immutable merge commit")
        if run.state == "pr_open" or (
            run.state == "publishing" and run.approval_evidence_sha256 is None
        ):
            if run.planning_commit is None or run.pull_request_number is None:
                raise LifecycleEventRejected(
                    "planning PR has no durable head and pull-request binding"
                )
            if run.approved_commit is not None and run.approved_commit != merge_sha:
                raise LifecycleEventRejected(
                    "legacy publication merge does not match its durable binding"
                )
            evidence = await self._github.pull_request_evidence(
                repository,
                run.pull_request_number,
                expected_head_sha=run.planning_commit,
            )
            if not evidence.merged or evidence.merge_commit_sha != merge_sha:
                raise LifecycleEventRejected(
                    "GitHub merge evidence does not match the planning webhook"
                )
            if evidence.approvals < 1:
                raise LifecycleEventRejected("planning PR has no current human approval")
            if not evidence.required_checks_pass(set(_REQUIRED_PLANNING_CHECKS)):
                raise LifecycleEventRejected(
                    "planning PR required validator check is not successful"
                )
            evidence_bytes = json.dumps(
                evidence.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            run = await run_sync_to_completion(
                self._store.approve_for_publication,
                run,
                merge_commit=merge_sha,
                evidence_sha256=hashlib.sha256(evidence_bytes).hexdigest(),
            )
        elif (
            run.state != "publishing"
            or run.approved_commit != merge_sha
            or run.approval_evidence_sha256 is None
        ):
            raise LifecycleEventRejected(
                "planning publication has no durable verified approval evidence"
            )
        binding = await self._github.artifact_binding(
            repository=repository,
            commit_sha=merge_sha,
            path=run.backlog_path,
        )
        raw = await self._github.read_immutable_artifact(binding)
        artifact = load_artifact_bytes(raw)
        if (
            artifact.plan.plan.id != run.plan_id
            or artifact.plan.plan.version != run.plan_version
            or artifact.plan.plan.source_idea.work_package_id != run.idea_id
            or artifact.plan.plan.publication_identity != f"{run.plan_id}:v{run.plan_version}"
        ):
            raise LifecycleEventRejected("merged backlog conflicts with its durable plan run")
        run = await run_sync_to_completion(
            self._store.record_publication_binding,
            run,
            approved_commit=merge_sha,
            backlog_blob_sha1=binding.blob_sha,
            backlog_sha256=artifact.sha256,
        )
        envelope = PublicationEnvelope(
            approved_commit=merge_sha,
            backlog_sha256=artifact.sha256,
            artifact_blob_sha1=binding.blob_sha,
            approval_event_id=event.source_delivery_id,
            snapshot_sha256=run.snapshot_sha256,
            snapshot_etag=run.snapshot_etag,
            trace_id=str(event.trace_id),
            publication_target_sha256=self._openproject.publication_target_sha256,
            publication_identity=artifact.plan.plan.publication_identity,
        )
        replan_context = await self._replan_publication_context(run, artifact)
        journal = self._publication_journal_factory()
        try:
            resumed = await run_sync_to_completion(journal.resume, envelope)
            if resumed is None:
                dry_run = await run_sync_to_completion(
                    publish,
                    artifact,
                    self._openproject,
                    envelope,
                    replan_context=replan_context,
                )
            else:
                # A durable journal is the authority after the first mutation
                # intent. Replaying it must precede a fresh stale-base check.
                dry_run = None
            result = await run_sync_to_completion(
                publish,
                artifact,
                self._openproject,
                envelope,
                apply=True,
                journal=journal,
                replan_context=replan_context,
            )
        except (
            OpenProjectConflict,
            PublicationJournalMismatch,
            PublicationRejected,
        ) as error:
            journal.close()
            blocked = await run_sync_to_completion(self._store.set_state, run, "blocked")
            await run_sync_to_completion(
                self._openproject.set_lifecycle_state,
                run.idea_id,
                status="Blocked",
                evidence_state="publication_conflict",
            )
            await run_sync_to_completion(
                self._openproject.ensure_comment,
                run.idea_id,
                (
                    "Publication is blocked and no approved managed field was "
                    f"overwritten. Conflict: {error}"
                ),
                idempotency_key=f"publication-blocked:{event.source_delivery_id}",
            )
            return await self._outcome(
                event,
                "publish_openproject_graph",
                "blocked",
                plan_id=blocked.plan_id,
                plan_version=blocked.plan_version,
                work_package_id=blocked.idea_id,
                pull_request_number=number,
            )
        except BaseException:
            journal.close()
            raise
        else:
            journal.close()
        if dry_run is not None and dry_run.operations != result.operations:
            raise LifecycleEventRejected("publication apply diverged from its approved dry-run")
        published = await run_sync_to_completion(self._store.set_state, run, "published")
        await run_sync_to_completion(
            self._openproject.ensure_comment,
            run.idea_id,
            (
                f"Published `{run.plan_id}:v{run.plan_version}` from merge `{merge_sha}` "
                f"with {len(result.operations)} deterministic operations."
            ),
            idempotency_key=f"publication:{event.source_delivery_id}",
        )
        return await self._outcome(
            event,
            "publish_openproject_graph",
            "published",
            plan_id=published.plan_id,
            plan_version=published.plan_version,
            work_package_id=published.idea_id,
            pull_request_number=number,
            operation_count=len(result.operations),
            applied_operation_count=len(result.applied_operations),
            resumed=resumed is not None,
        )

    async def _implementation_pull_request(
        self,
        event: EventEnvelope,
        repository: str,
        pull: dict[str, Any],
    ) -> LifecycleOutcome:
        body = pull.get("body")
        marker = re.search(
            r"<!-- planning-platform:work-package "
            r"plan_id=([a-z0-9][a-z0-9-]{2,63}) "
            r"node_key=([a-z0-9][a-z0-9._-]{0,127}) -->",
            body if isinstance(body, str) else "",
        )
        if marker is None:
            return await self._outcome(event, "implementation_pr", "unmanaged")
        identity = marker.group(1), marker.group(2)
        number = self._positive_int(pull.get("number"), "pull request")
        head = pull.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise LifecycleEventRejected("implementation PR has no immutable head commit")
        url = pull.get("html_url")
        if url != f"https://github.com/{repository}/pull/{number}":
            raise LifecycleEventRejected("implementation PR has no canonical GitHub URL")
        pull_request_state = pull.get("state")
        if pull_request_state not in {"open", "closed"}:
            raise LifecycleEventRejected("implementation PR has no open or closed state")
        merged = pull.get("merged") is True
        merge_commit = pull.get("merge_commit_sha") if merged else None
        if merged and (
            not isinstance(merge_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", merge_commit)
        ):
            raise LifecycleEventRejected("merged implementation PR has no merge commit")
        if merged and pull_request_state != "closed":
            raise LifecycleEventRejected("merged implementation PR is not closed")
        existing = await run_sync_to_completion(
            self._store.by_implementation_pull_request,
            repository,
            number,
        )
        if (
            existing is not None
            and (
                existing.plan_id,
                existing.node_key,
            )
            != identity
        ):
            raise LifecycleStoreMismatch(
                "implementation PR replay changed its work-package identity"
            )
        package = await run_sync_to_completion(self._openproject.resolve, identity)
        if package is None:
            raise LifecycleEventRejected("implementation PR names an unknown work package")
        if existing is None:
            active = await run_sync_to_completion(
                self._store.latest_published,
                identity[0],
            )
            active_plan = None if active is None else await self._published_plan(active)
            active_item = (
                None if active_plan is None else active_plan.by_key.get(identity[1])
            )
            expected_version = None
            expected_hash = None
            if (
                active is not None
                and active.approved_commit is not None
                and active_plan is not None
                and active_item is not None
            ):
                expected_version, _expected_commit = effective_node_binding(
                    active_plan,
                    active.approved_commit,
                    active_item.key,
                )
                expected_hash = managed_hash(
                    with_approved_commit(active_plan, active.approved_commit),
                    active_item,
                )
            terminal_ids = {
                self._openproject.config.status_ids[name]
                for name in ("Done", "Superseded", "Rejected")
            }
            if (
                active is None
                or active_item is None
                or package.plan_version != expected_version
                or package.managed_hash != expected_hash
                or active_item.repository != repository
                or package.repository != active_item.repository
                or package.human_fields.get("status_id") in terminal_ids
            ):
                raise LifecycleEventRejected(
                    "implementation PR does not match an active approved repository node"
                )
        elif package.id != existing.work_package_id:
            raise LifecycleStoreMismatch(
                "implementation PR work-package binding no longer resolves"
            )
        association = await run_sync_to_completion(
            self._store.bind_implementation_pull_request,
            repository=repository,
            number=number,
            plan_id=identity[0],
            node_key=identity[1],
            work_package_id=package.id,
            url=url,
            head_sha=head_sha,
            observed_at=event.occurred_at,
            pull_request_state=pull_request_state,
            merged_commit=merge_commit,
        )
        await run_sync_to_completion(
            self._openproject.ensure_comment,
            package.id,
            f"Implementation pull request: {url}",
            idempotency_key=(
                "implementation-pr:"
                + hashlib.sha256(f"{repository}#{number}".encode()).hexdigest()[:32]
            ),
        )
        await self._project_implementation_work_package(association.work_package_id)
        return await self._outcome(
            event,
            "implementation_pr",
            ("merged" if association.merged_commit is not None else association.pull_request_state),
            plan_id=identity[0],
            work_package_id=package.id,
            pull_request_number=number,
        )

    async def _published_artifact(self, run: PlanRun) -> LoadedArtifact:
        if (
            run.state != "published"
            or run.approved_commit is None
            or run.approval_evidence_sha256 is None
            or run.backlog_blob_sha1 is None
            or run.backlog_sha256 is None
        ):
            raise LifecycleEventRejected(
                "active published plan is missing immutable artifact bindings"
            )
        binding = ImmutableArtifactBinding(
            repository=run.repository,
            commit_sha=run.approved_commit,
            path=run.backlog_path,
            blob_sha=run.backlog_blob_sha1,
            content_sha256=run.backlog_sha256,
        )
        artifact = load_artifact_bytes(
            await self._github.read_immutable_artifact(binding)
        )
        if (
            artifact.plan.plan.id != run.plan_id
            or artifact.plan.plan.version != run.plan_version
            or artifact.plan.plan.source_idea.work_package_id != run.idea_id
            or artifact.blob_sha1 != binding.blob_sha
            or artifact.sha256 != binding.content_sha256
        ):
            raise LifecycleEventRejected(
                "active published artifact does not match its lifecycle binding"
            )
        return artifact

    async def _published_plan(self, run: PlanRun) -> BacklogPlan:
        return (await self._published_artifact(run)).plan

    async def _replan_publication_context(
        self,
        run: PlanRun,
        artifact: LoadedArtifact,
    ) -> ReplanPublicationContext | None:
        request = self._recover_start_request(run)
        scope = artifact.plan.plan.replan
        authorized = request.replan
        if (
            request.plan_id != run.plan_id
            or request.plan_version != run.plan_version
            or request.idea.work_package_id != run.idea_id
            or (scope is None) != (authorized is None)
        ):
            raise LifecycleEventRejected(
                "planning publication changed its durable operator authorization"
            )
        if scope is None or authorized is None:
            return None
        base = await run_sync_to_completion(
            self._store.get,
            artifact.plan.plan.id,
            scope.base_plan_version,
        )
        latest = await run_sync_to_completion(
            self._store.latest_published,
            artifact.plan.plan.id,
        )
        if base is None or base != latest or base.approved_commit is None:
            raise LifecycleEventRejected(
                "partial replan publication base is not the latest immutable published plan"
            )
        if (
            authorized.prior_plan.plan.id != artifact.plan.plan.id
            or authorized.prior_plan.plan.version != scope.base_plan_version
            or authorized.base_approved_planning_commit != base.approved_commit
        ):
            raise LifecycleEventRejected(
                "partial replan publication changed its durable operator authorization"
            )
        base_artifact = await self._published_artifact(base)
        if authorized.prior_plan != base_artifact.plan:
            raise LifecycleEventRejected(
                "partial replan publication base differs from its durable operator request"
            )
        return ReplanPublicationContext(
            base_artifact=base_artifact,
            base_approved_commit=base.approved_commit,
            selected_root_keys=authorized.selected_root_keys,
            affected_node_keys=authorized.affected_node_keys,
        )

    async def _check_run(self, event: EventEnvelope) -> LifecycleOutcome:
        check = self._payload_object(event.payload, "check_run")
        pulls = check.get("pull_requests")
        if not isinstance(pulls, list) or not pulls:
            return await self._outcome(event, "check_run", "no_pull_request")
        repository = self._payload_object(event.payload, "repository").get("full_name")
        if not isinstance(repository, str):
            raise LifecycleEventRejected("GitHub check run has no repository identity")
        head_sha = check.get("head_sha")
        if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise LifecycleEventRejected("GitHub check run has no immutable head commit")
        check_id = check.get("id")
        check_name = check.get("name")
        check_app = check.get("app")
        if (
            type(check_id) is not int
            or check_id <= 0
            or not isinstance(check_name, str)
            or not isinstance(check_app, dict)
            or type(check_app.get("id")) is not int
            or not isinstance(check_app.get("slug"), str)
        ):
            raise LifecycleEventRejected("GitHub check run has no exact check identity")
        advanced = 0
        seen: set[int] = set()
        for reference in pulls:
            if not isinstance(reference, dict):
                continue
            number = reference.get("number")
            if type(number) is not int or number <= 0 or number in seen:
                continue
            seen.add(number)
            # Planning-PR checks are approval evidence only; merge remains the
            # publication gate. Implementation associations arrive through PR
            # events and advance to Review, never directly to Done.
            run = await run_sync_to_completion(self._store.by_pull_request, repository, number)
            if run is not None:
                advanced += 1
                continue
            association = await run_sync_to_completion(
                self._store.by_implementation_pull_request,
                repository,
                number,
            )
            if association is None or association.head_sha != head_sha:
                continue
            evidence = await self._github.pull_request_evidence(
                repository,
                number,
                expected_head_sha=head_sha,
            )
            association = await run_sync_to_completion(
                self._store.bind_implementation_pull_request,
                repository=repository,
                number=number,
                plan_id=association.plan_id,
                node_key=association.node_key,
                work_package_id=association.work_package_id,
                url=association.pull_request_url,
                head_sha=evidence.head_sha,
                observed_at=event.occurred_at,
                pull_request_state=evidence.state,
                merged_commit=evidence.merge_commit_sha if evidence.merged else None,
            )
            passed = self._implementation_evidence_passes(evidence)
            association = await run_sync_to_completion(
                self._store.record_implementation_check_result,
                repository,
                number,
                head_sha=head_sha,
                passed=passed,
            )
            if association is not None:
                await self._project_implementation_work_package(association.work_package_id)
                advanced += int(passed)
        return await self._outcome(event, "check_run", f"successful:{advanced}")

    async def _reconcile(self, event: EventEnvelope) -> LifecycleOutcome:
        runs = await run_sync_to_completion(self._store.all)
        latest_by_plan: dict[str, PlanRun] = {}
        for candidate in runs:
            latest = latest_by_plan.get(candidate.plan_id)
            if latest is None or candidate.plan_version > latest.plan_version:
                latest_by_plan[candidate.plan_id] = candidate
        latest_runs = tuple(latest_by_plan.values())
        latest_published_by_plan: dict[str, PlanRun] = {}
        for candidate in runs:
            if candidate.state != "published":
                continue
            latest = latest_published_by_plan.get(candidate.plan_id)
            if latest is None or candidate.plan_version > latest.plan_version:
                latest_published_by_plan[candidate.plan_id] = candidate
        active_graph_runs = tuple(latest_published_by_plan.values())
        repaired_merges = 0
        for run in latest_runs:
            if (
                run.state != "pr_open"
                or run.pull_request_number is None
                or run.planning_commit is None
            ):
                continue
            evidence = await self._github.pull_request_evidence(
                run.repository,
                run.pull_request_number,
                expected_head_sha=run.planning_commit,
            )
            if not evidence.merged or evidence.merge_commit_sha is None:
                continue
            repair_event = envelope_for_delivery(
                event_type="github.pull_request",
                source="windmill",
                delivery_id=(
                    f"reconcile:{event.source_delivery_id}:"
                    f"{run.repository}:{run.pull_request_number}"
                ),
                occurred_at=event.occurred_at,
                received_at=event.received_at,
                actor=EventActor(kind="system", id="windmill-reconciler"),
                subject=EventSubject(
                    idea_id=run.idea_id,
                    plan_id=run.plan_id,
                    plan_version=run.plan_version,
                ),
                signature=VerifiedSignature(verified=True, algorithm="internal"),
                payload={
                    "pull_request": {
                        "number": run.pull_request_number,
                        "merged": True,
                        "merge_commit_sha": evidence.merge_commit_sha,
                    },
                    "repository": {"full_name": run.repository},
                },
            )
            outcome = await self._pull_request(repair_event)
            if outcome.outcome in {"published", "already_published"}:
                repaired_merges += 1

        repaired_implementation = 0
        implementation_failures = 0
        associations = await run_sync_to_completion(self._store.implementation_associations)
        for association in associations:
            try:
                evidence = await self._github.pull_request_evidence(
                    association.repository,
                    association.pull_request_number,
                )
                await run_sync_to_completion(
                    self._store.bind_implementation_pull_request,
                    repository=association.repository,
                    number=association.pull_request_number,
                    plan_id=association.plan_id,
                    node_key=association.node_key,
                    work_package_id=association.work_package_id,
                    url=association.pull_request_url,
                    head_sha=evidence.head_sha,
                    observed_at=event.occurred_at,
                    pull_request_state=evidence.state,
                    merged_commit=(evidence.merge_commit_sha if evidence.merged else None),
                )
                await run_sync_to_completion(
                    self._store.record_implementation_check_result,
                    association.repository,
                    association.pull_request_number,
                    head_sha=evidence.head_sha,
                    passed=self._implementation_evidence_passes(evidence),
                )
            except (GitHubAdapterError, LifecycleStoreMismatch):
                implementation_failures += 1
                continue

        refreshed_associations = await run_sync_to_completion(
            self._store.implementation_associations
        )
        for work_package_id in sorted(
            {association.work_package_id for association in refreshed_associations}
        ):
            try:
                if await self._project_implementation_work_package(work_package_id):
                    repaired_implementation += 1
            except (LifecycleStoreMismatch, OpenProjectPublicationError):
                implementation_failures += 1
                continue

        current = await run_sync_to_completion(self._openproject.snapshot)
        try:
            identities = current.identities()
        except ValueError:
            return await self._outcome(
                event,
                "nightly_reconciliation",
                "identity_conflict",
            )
        stale_threads = await run_sync_to_completion(
            self._store.stale_thread_count,
            event.occurred_at - self._planning_thread_stale_after,
        )
        findings = implementation_failures + stale_threads
        safe_repairs = 0
        status_ids = self._openproject.config.status_ids
        done_id = status_ids["Done"]
        blocked_id = status_ids["Blocked"]
        ready_id = status_ids["Ready"]
        terminal_ids = {
            done_id,
            status_ids["Superseded"],
            status_ids["Rejected"],
        }
        packages_by_id = {package.id: package for package in current.work_packages}
        for work_package_id in {
            association.work_package_id for association in refreshed_associations
        }:
            package = packages_by_id.get(work_package_id)
            relevant = tuple(
                association
                for association in refreshed_associations
                if association.work_package_id == work_package_id
            )
            if (
                package is not None
                and package.human_fields.get("status_id") == status_ids["In Progress"]
                and relevant
                and not any(
                    association.pull_request_state == "open"
                    or association.merged_commit is not None
                    for association in relevant
                )
                and event.occurred_at
                - max(association.head_observed_at for association in relevant)
                >= self._implementation_stale_after
            ):
                findings += 1
        for run in latest_runs:
            if run.state == "needs_input":
                planner_state = await self._planner.get(run.thread_id)
                idea = next(
                    (package for package in current.work_packages if package.id == run.idea_id),
                    None,
                )
                if planner_state.interrupt is not None and (
                    idea is None or idea.human_fields.get("status_id") != status_ids["Needs Input"]
                ):
                    await run_sync_to_completion(
                        self._openproject.set_lifecycle_state,
                        run.idea_id,
                        status="Needs Input",
                    )
                    safe_repairs += 1
        for run in active_graph_runs:
            if (
                run.approved_commit is None
                or run.backlog_blob_sha1 is None
                or run.backlog_sha256 is None
            ):
                findings += 1
                continue
            binding = ImmutableArtifactBinding(
                repository=run.repository,
                commit_sha=run.approved_commit,
                path=run.backlog_path,
                blob_sha=run.backlog_blob_sha1,
                content_sha256=run.backlog_sha256,
            )
            artifact = load_artifact_bytes(await self._github.read_immutable_artifact(binding))
            approved = with_approved_commit(artifact.plan, run.approved_commit)
            operations = plan_diff(
                approved,
                current,
                trace_id=str(event.trace_id),
            )
            findings += sum(operation.kind != "record_audit" for operation in operations)
            for repository in approved.plan.repositories:
                _branch, head = await self._github.repository_head(repository.name)
                if head != repository.commit:
                    findings += 1

            for item in approved.items:
                package = identities.get((approved.plan.id, item.key))
                if package is None:
                    continue
                blocker_states = [
                    identities.get((approved.plan.id, blocker)) for blocker in item.blocked_by
                ]
                open_blocker = any(
                    blocker is None
                    or blocker.superseded
                    or blocker.human_fields.get("status_id") != done_id
                    for blocker in blocker_states
                )
                status_id = package.human_fields.get("status_id")
                if status_id == blocked_id and item.blocked_by and not open_blocker:
                    await run_sync_to_completion(
                        self._openproject.set_lifecycle_state,
                        package.id,
                        status="Ready",
                    )
                    safe_repairs += 1
                elif status_id == ready_id and open_blocker:
                    await run_sync_to_completion(
                        self._openproject.set_lifecycle_state,
                        package.id,
                        status="Blocked",
                    )
                    safe_repairs += 1
                if status_id == done_id and package.evidence_state not in {
                    "completed",
                    "deployed",
                    "pr_merged",
                    "verified",
                }:
                    findings += 1
                if any(
                    (blocker := identities.get((approved.plan.id, key))) is not None
                    and blocker.superseded
                    for key in item.blocked_by
                ):
                    findings += 1
                children = [
                    identities.get((approved.plan.id, child.key))
                    for child in approved.items
                    if child.parent == item.key
                ]
                if status_id == done_id and any(
                    child is None or child.human_fields.get("status_id") not in terminal_ids
                    for child in children
                ):
                    findings += 1
        return await self._outcome(
            event,
            "nightly_reconciliation",
            (
                f"inspected:{len(latest_runs)}:findings:{findings}:"
                f"repairs:{safe_repairs}:missed_merges:{repaired_merges}:"
                f"implementation_repairs:{repaired_implementation}:"
                f"implementation_failures:{implementation_failures}:"
                f"stale_threads:{stale_threads}"
            ),
        )

    async def _outcome(
        self,
        event: EventEnvelope,
        action: str,
        outcome: str,
        *,
        plan_id: str | None = None,
        plan_version: int | None = None,
        work_package_id: int | None = None,
        pull_request_number: int | None = None,
        operation_count: int | None = None,
        applied_operation_count: int | None = None,
        resumed: bool | None = None,
    ) -> LifecycleOutcome:
        result = LifecycleOutcome(
            action=action,
            outcome=outcome,
            plan_id=plan_id,
            plan_version=plan_version,
            work_package_id=work_package_id,
            pull_request_number=pull_request_number,
            operation_count=operation_count,
            applied_operation_count=applied_operation_count,
            resumed=resumed,
        )
        await run_sync_to_completion(
            self._store.audit,
            event_id=str(event.event_id),
            trace_id=str(event.trace_id),
            action=action,
            outcome=outcome,
            details={key: value for key, value in result.__dict__.items() if value is not None},
        )
        return result

    @staticmethod
    def _payload_object(payload: dict[str, Any], key: str) -> dict[str, Any]:
        value = payload.get(key)
        if not isinstance(value, dict):
            raise LifecycleEventRejected(f"event payload has no {key} object")
        return value

    @staticmethod
    def _prior_descendant_keys(prior: Any, roots: set[str]) -> tuple[str, ...]:
        """Keep caller root order, then prior-plan order for the bounded closure."""
        closure = set(roots)
        while True:
            expanded = closure | {item.key for item in prior.items if item.parent in closure}
            if expanded == closure:
                return tuple(item.key for item in prior.items if item.key in closure)
            closure = expanded

    @staticmethod
    def _link_title(value: dict[str, Any], key: str) -> str:
        links = value.get("_links")
        link = links.get(key) if isinstance(links, dict) else None
        title = link.get("title") if isinstance(link, dict) else None
        if not isinstance(title, str):
            raise LifecycleEventRejected(f"work package has no {key} title")
        return title

    @staticmethod
    def _description(value: Mapping[str, Any]) -> str:
        description = value.get("description", "")
        if isinstance(description, dict):
            description = description.get("raw", "")
        return str(description or "")

    @staticmethod
    def _repositories(description: str) -> tuple[str, ...]:
        lines = _visible_markdown_lines(description)
        sections: list[str] = []
        for index, line in enumerate(lines):
            if (
                line is None
                or _RELEVANT_REPOSITORIES_HEADING.fullmatch(line) is None
            ):
                continue
            end = next(
                (
                    candidate
                    for candidate in range(index + 1, len(lines))
                    if _markdown_section_boundary(lines[candidate])
                ),
                len(lines),
            )
            sections.append(
                "\n".join(
                    candidate
                    for candidate in lines[index + 1 : end]
                    if candidate is not None
                )
            )
        if len(sections) != 1:
            return ()

        result: list[str] = []
        for line in sections[0].splitlines():
            value = _repository_entry(line)
            if value is None:
                continue
            if value not in result:
                result.append(value)
        return tuple(result[:20])

    def _implementation_evidence_passes(
        self,
        evidence: PullRequestEvidence,
    ) -> bool:
        required = self._implementation_required_checks.get(evidence.repository)
        return required is not None and evidence.required_checks_pass(set(required))

    async def _project_implementation_work_package(
        self,
        work_package_id: int,
    ) -> bool:
        associations = tuple(
            association
            for association in await run_sync_to_completion(self._store.implementation_associations)
            if association.work_package_id == work_package_id
        )
        if not associations:
            raise LifecycleStoreMismatch("implementation projection has no durable associations")
        identities = {(association.plan_id, association.node_key) for association in associations}
        if len(identities) != 1:
            raise LifecycleStoreMismatch(
                "implementation work package has conflicting plan identities"
            )
        identity = next(iter(identities))
        package = await run_sync_to_completion(self._openproject.resolve, identity)
        if package is None or package.id != work_package_id:
            raise LifecycleStoreMismatch(
                "implementation projection work package no longer resolves"
            )
        desired_status, evidence_state = self._implementation_projection(associations)
        status: str | None = None
        if desired_status is not None:
            status_ids = self._openproject.config.status_ids
            current_id = package.human_fields.get("status_id")
            ranks = {
                status_ids["Proposed"]: 0,
                status_ids["Ready"]: 0,
                status_ids["In Progress"]: 1,
                status_ids["Review"]: 2,
            }
            desired_id = status_ids[desired_status]
            current_rank = ranks.get(current_id) if type(current_id) is int else None
            desired_rank = ranks[desired_id]
            if current_rank is not None and desired_rank > current_rank:
                status = desired_status
        evidence = evidence_state if package.evidence_state != evidence_state else None
        if status is None and evidence is None:
            return False
        await run_sync_to_completion(
            self._openproject.set_lifecycle_state,
            work_package_id,
            status=status,
            evidence_state=evidence,
        )
        return True

    @staticmethod
    def _implementation_projection(
        associations: tuple[ImplementationPrAssociation, ...],
    ) -> tuple[str | None, str]:
        if any(association.merged_commit is not None for association in associations):
            return "Review", "pr_merged"
        if any(
            association.pull_request_state == "open"
            and association.successful_check_sha == association.head_sha
            for association in associations
        ):
            return "Review", "check_passed"
        if any(association.pull_request_state == "open" for association in associations):
            return "In Progress", "pr_open"
        return None, "pr_closed"

    @staticmethod
    def _positive_int(value: object, label: str) -> int:
        if type(value) is not int or value <= 0:
            raise LifecycleEventRejected(f"{label} must be a positive integer")
        return value

    @staticmethod
    def _nonnegative_int(value: object, label: str) -> int:
        if type(value) is not int or value < 0:
            raise LifecycleEventRejected(f"{label} must be a nonnegative integer")
        return value

    @staticmethod
    def _timestamp(value: object, label: str) -> datetime:
        if not isinstance(value, str):
            raise LifecycleEventRejected(f"{label} must be an RFC3339 timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise LifecycleEventRejected(f"{label} must be an RFC3339 timestamp") from error
        if parsed.tzinfo is None:
            raise LifecycleEventRejected(f"{label} must include a timezone")
        return parsed.astimezone(UTC)
