from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_workflow_matches_package_version_and_windmill_revision() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    workflow = (ROOT / ".github/workflows/release-images.yml").read_text(encoding="utf-8")
    planner_dockerfile = (ROOT / "apps/planner-api/Dockerfile").read_text(encoding="utf-8")
    windmill_dockerfile = (ROOT / "deploy/windmill/extend.Dockerfile").read_text(
        encoding="utf-8"
    )

    assert re.search(rf"(?m)^\s+- v{re.escape(version)}$", workflow)
    assert re.search(rf"(?m)^\s+PLANNER_TAG: v{re.escape(version)}$", workflow)
    assert workflow.count(f"PLANNING_PLATFORM_VERSION={version}") == 2
    assert f'version("planning-platform") == "{version}"' in workflow
    assert f"ARG PLANNING_PLATFORM_VERSION={version}" in planner_dockerfile
    assert '"planning-platform==${PLANNING_PLATFORM_VERSION}"' in planner_dockerfile
    assert f"ARG PLANNING_PLATFORM_VERSION={version}" in windmill_dockerfile
    assert (
        '"/tmp/planning-platform-dist/'
        'planning_platform-${PLANNING_PLATFORM_VERSION}-py3-none-any.whl"'
        in windmill_dockerfile
    )

    runtime = re.search(r"(?m)^\s+WINDMILL_TAG: v1\.775\.2-planning\.(\d+)$", workflow)
    source = re.search(r"(?m)^\s+WINDMILL_SOURCE_TAG: v1\.775\.2-ce-source\.(\d+)$", workflow)
    assert runtime is not None
    assert source is not None
    assert runtime.group(1) == source.group(1)
