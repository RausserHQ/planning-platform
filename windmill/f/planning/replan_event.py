"""Build a trusted bounded-replan envelope from the Windmill root job."""

from __future__ import annotations

import os
from typing import Any

from planning_platform.lifecycle.ingress import replan_affected_subgraph_envelope


def main(
    plan_id: str,
    base_plan_version: int,
    affected_node_keys: list[str],
    reason: str,
) -> dict[str, Any]:
    stable_delivery = os.environ.get("WM_ROOT_FLOW_JOB_ID") or os.environ.get("WM_JOB_ID")
    if not stable_delivery:
        raise RuntimeError("Windmill job identity is required for bounded replan")
    return replan_affected_subgraph_envelope(
        plan_id=plan_id,
        base_plan_version=base_plan_version,
        affected_node_keys=tuple(affected_node_keys),
        reason=reason,
        delivery_id=stable_delivery,
    ).model_dump(mode="json")
