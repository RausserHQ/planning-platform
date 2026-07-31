"""Windmill-callable runners; publication semantics remain centralized in core modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from planning_platform.loader import LoadedArtifact
from planning_platform.publisher import PublicationEnvelope, PublishResult, publish
from planning_platform.reconciliation import ReconciliationReport, reconcile

if TYPE_CHECKING:
    from planning_platform.openproject import OpenProjectSnapshot
    from planning_platform.publication_journal import PublicationJournal
    from planning_platform.publisher import PublicationAdapter


class LifecycleGateRejected(ValueError):
    pass


@dataclass(frozen=True)
class PublicationCommand:
    """A merge-gated command; a proposal alone can never set ``approved``."""

    artifact: LoadedArtifact
    envelope: PublicationEnvelope
    approved_by_merge: bool
    merge_commit: str

    def __post_init__(self) -> None:
        if not self.approved_by_merge:
            raise LifecycleGateRejected("OpenProject publication requires a merged planning PR")
        if self.merge_commit != self.envelope.approved_commit:
            raise LifecycleGateRejected("merge commit and immutable publication commit differ")


class PublicationRunner:
    """Delegates the effect to the one publisher implementation; never rebuilds a diff."""

    def run(
        self,
        command: PublicationCommand,
        adapter: PublicationAdapter,
        journal: PublicationJournal,
    ) -> PublishResult:
        return publish(
            command.artifact,
            adapter,
            command.envelope,
            apply=True,
            journal=journal,
        )


class ReconciliationRunner:
    """Returns deterministic safe repairs for a separately approved lifecycle action."""

    def run(
        self,
        artifact: LoadedArtifact,
        snapshot: OpenProjectSnapshot,
        *,
        approved_commit: str,
        trace_id: str,
    ) -> ReconciliationReport:
        return reconcile(
            artifact.plan,
            snapshot,
            approved_commit=approved_commit,
            trace_id=trace_id,
        )
