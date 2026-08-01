"""Build a trusted convergence-proof envelope from the Windmill root job."""

from __future__ import annotations

import os
from typing import Any

from planning_platform.lifecycle.ingress import convergence_check_envelope


def main(plan_id: str, plan_version: int) -> dict[str, Any]:
    stable_delivery = os.environ.get("WM_ROOT_FLOW_JOB_ID") or os.environ.get("WM_JOB_ID")
    if not stable_delivery:
        raise RuntimeError("Windmill job identity is required for convergence proof")
    return convergence_check_envelope(
        plan_id=plan_id,
        plan_version=plan_version,
        delivery_id=stable_delivery,
    ).model_dump(mode="json")
