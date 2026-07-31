"""Read-only reconciliation and narrowly scoped safe repair proposals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .diff import PublicationOperation, plan_diff
from .models import BacklogPlan, with_approved_commit
from .openproject import OpenProjectSnapshot, managed_hash


@dataclass(frozen=True)
class ReconciliationFinding:
    code: Literal[
        "missing",
        "managed_drift",
        "superseded",
        "stale_snapshot",
        "identity_conflict",
        "unapproved_plan",
    ]
    message: str
    node_key: str | None = None
    safe_repair: bool = False


@dataclass(frozen=True)
class ReconciliationReport:
    findings: tuple[ReconciliationFinding, ...]
    safe_repairs: tuple[PublicationOperation, ...]


def reconcile(
    plan: BacklogPlan,
    snapshot: OpenProjectSnapshot,
    trace_id: str = "reconcile",
    *,
    approved_commit: str | None = None,
) -> ReconciliationReport:
    findings: list[ReconciliationFinding] = []
    effective_commit = approved_commit or plan.plan.approved_planning_commit
    if effective_commit is None:
        return ReconciliationReport(
            (
                ReconciliationFinding(
                    "unapproved_plan",
                    "reconciliation requires the immutable approved planning commit",
                ),
            ),
            (),
        )
    plan = with_approved_commit(plan, effective_commit)
    try:
        identities = snapshot.identities()
    except ValueError as error:
        return ReconciliationReport((ReconciliationFinding("identity_conflict", str(error)),), ())
    if (
        snapshot.sha256 != plan.plan.openproject_snapshot.sha256
        or snapshot.etag != plan.plan.openproject_snapshot.etag
    ):
        findings.append(
            ReconciliationFinding("stale_snapshot", "snapshot differs from plan publication base")
        )
        return ReconciliationReport(tuple(findings), ())
    for item in plan.items:
        package = identities.get((plan.plan.id, item.key))
        if package is None:
            findings.append(
                ReconciliationFinding("missing", "managed work package is missing", item.key, True)
            )
        elif package.managed_hash != managed_hash(plan, item) or package.superseded:
            findings.append(
                ReconciliationFinding(
                    "managed_drift", "managed fields differ from plan", item.key, True
                )
            )
    desired = {item.key for item in plan.items}
    for identity, package in identities.items():
        if identity[0] == plan.plan.id and identity[1] not in desired and not package.superseded:
            findings.append(
                ReconciliationFinding(
                    "superseded", "removed plan node needs superseding", identity[1], True
                )
            )
    repairs = plan_diff(plan, snapshot, trace_id=trace_id)
    return ReconciliationReport(tuple(findings), repairs)
