"""Mermaid rendering for the semantically distinct planning relations."""

from __future__ import annotations

import re

from .models import BacklogPlan


def _label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9 _.-]", "", value).replace('"', "")


def render_mermaid(plan: BacklogPlan) -> str:
    """Render a stable graph without turning mutex groups into blockers."""
    lines = ["flowchart TD"]
    for item in sorted(plan.items, key=lambda item: item.key):
        lines.append(f'    {item.key}["{_label(item.title)}"]')
    edges: list[tuple[str, str, str]] = []
    for item in plan.items:
        if item.parent:
            edges.append((item.parent, item.key, "contains"))
        edges.extend((dependency, item.key, "blocks") for dependency in item.blocked_by)
        edges.extend(
            (dependency, item.key, "preferred order") for dependency in item.sequence_after
        )
        edges.extend((item.key, related, "related") for related in item.related_to)
        edges.extend(
            (decision, item.key, "decision required") for decision in item.decision_required
        )
        edges.extend((decision, item.key, "governs") for decision in item.decisions)
    for source, target, label in sorted(edges):
        lines.append(f"    {source} -->|{label}| {target}")
    return "\n".join(lines) + "\n"
