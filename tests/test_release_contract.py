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
    assert windmill_dockerfile.startswith(
        "ARG WINDMILL_BASE=ghcr.io/windmill-labs/windmill:1.775.2@"
        "sha256:ef39329523f4806e5cd5169ffa7af2618f39439bcf659115e8bb804c592d7132\n"
    )
    assert (
        '"/tmp/planning-platform-dist/'
        'planning_platform-${PLANNING_PLATFORM_VERSION}-py3-none-any.whl"'
        in windmill_dockerfile
    )

    assert re.search(
        r"(?m)^\s+WINDMILL_TAG: v1\.775\.2-planning\.16$",
        workflow,
    )
    assert (
        "WINDMILL_BASE: ghcr.io/windmill-labs/windmill:1.775.2@"
        "sha256:ef39329523f4806e5cd5169ffa7af2618f39439bcf659115e8bb804c592d7132"
        in workflow
    )
    assert "WINDMILL_BASE=${{ env.WINDMILL_BASE }}" in workflow
    assert "{{.Image.OS}}/{{.Image.Architecture}}" in workflow
    assert '"linux/amd64"' in workflow
    assert "org.opencontainers.image.version" in workflow
    assert "org.opencontainers.image.revision" in workflow
    assert "org.opencontainers.image.source" in workflow
    assert "grep -F 'features=ce'" in workflow
    assert "grep -F 'features=ee'" in workflow
    assert "Check out exact Windmill source" not in workflow
    assert "Build and push Windmill CE from exact source" not in workflow
    assert "WINDMILL_SOURCE_IMAGE" not in workflow
    assert "WINDMILL_SOURCE_TAG" not in workflow
