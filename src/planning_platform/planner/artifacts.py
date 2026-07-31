"""Deterministic planning-artifact rendering."""

from __future__ import annotations

import hashlib
from typing import Any

import yaml  # type: ignore[import-untyped]

from planning_platform.graph import render_mermaid
from planning_platform.models import BacklogPlan

from .models import ArtifactManifestEntry


def render_artifact_set(state: dict[str, Any], backlog: BacklogPlan) -> dict[str, str]:
    compact = state["compact_specification"]
    architecture = state["architecture"]
    requirements = state["requirements_draft"]
    spec = "\n".join(
        (
            "# Specification",
            "",
            "## Problem",
            str(compact["problem"]),
            "",
            "## Desired outcome",
            str(compact["desired_outcome"]),
            "",
            "## Requirements",
            *[f"- {value}" for value in requirements["requirements"]],
            "",
            "## Constraints",
            *[f"- {value}" for value in compact["constraints"]],
            "",
            "## Non-goals",
            *[f"- {value}" for value in compact["non_goals"]],
            "",
        )
    )
    artifacts = {
        "SPEC.md": spec,
        "ARCHITECTURE.md": f"# Architecture\n\n{architecture['body']}\n",
        "DECISIONS.md": _decisions(tuple(requirements["decisions"])),
        "backlog.yaml": yaml.safe_dump(
            backlog.model_dump(mode="json"), sort_keys=False, allow_unicode=True
        ),
        "backlog.mmd": render_mermaid(backlog),
        "VALIDATION.md": _validation(backlog),
    }
    prd = state.get("prd")
    if prd:
        artifacts["PRD.md"] = f"# Product Requirements\n\n{prd['body']}\n"
    return artifacts


def artifact_manifest(artifacts: dict[str, str]) -> tuple[ArtifactManifestEntry, ...]:
    return tuple(
        ArtifactManifestEntry(
            path=path,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        for path, content in sorted(artifacts.items())
    )


def _decisions(decisions: tuple[str, ...]) -> str:
    body = "\n".join(f"- {decision}" for decision in decisions) or "- No unresolved decisions."
    return f"# Decisions\n\n{body}\n"


def _validation(backlog: BacklogPlan) -> str:
    lines = ["# Validation", ""]
    for item in backlog.items:
        lines.append(f"## {item.key}")
        lines.extend(f"- `{command}`" for command in item.validation_commands)
        lines.append("")
    return "\n".join(lines)
