from __future__ import annotations

import json
from pathlib import Path

from planning_platform.cli import main
from planning_platform.graph import render_mermaid
from planning_platform.loader import load_plan

FIXTURE = Path(__file__).parents[1] / "evals/fixtures/single-repository/backlog.yaml"


def test_graph_and_validate_cli(capsys) -> None:
    plan = load_plan(FIXTURE)
    assert "flowchart TD" in render_mermaid(plan)
    assert main(["validate", str(FIXTURE)]) == 0
    assert main(["graph", str(FIXTURE), "--format", "mermaid"]) == 0
    assert "implement-core" in capsys.readouterr().out


def test_diff_and_dry_run_cli(tmp_path: Path, capsys) -> None:
    plan = load_plan(FIXTURE)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "captured_at": "now",
                "etag": "fixture",
                "sha256": plan.plan.openproject_snapshot.sha256,
                "work_packages": [],
            }
        )
    )
    assert main(["diff", str(FIXTURE), "--against-openproject", str(snapshot)]) == 0
    assert "create_work_package" in capsys.readouterr().out
    assert main(["publish", str(FIXTURE), "--against-openproject", str(snapshot), "--dry-run"]) == 0


def test_reconcile_cli_requires_matching_plan_id(tmp_path: Path) -> None:
    plan = load_plan(FIXTURE)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "captured_at": "now",
                "etag": "fixture",
                "sha256": plan.plan.openproject_snapshot.sha256,
                "work_packages": [],
            }
        )
    )
    command = [
        "reconcile",
        str(FIXTURE),
        "--against-openproject",
        str(snapshot),
        "--plan",
        plan.plan.id,
        "--approved-commit",
        "a" * 40,
    ]
    assert main(command) == 0
    command[-3] = "wrong-plan"
    assert main(command) == 2
