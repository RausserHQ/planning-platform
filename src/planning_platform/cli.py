"""Small local-only CLI for deterministic planning artifacts."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .diff import plan_diff
from .graph import render_mermaid
from .loader import SchemaValidationError, load_plan
from .openproject import OpenProjectSnapshot
from .reconciliation import reconcile
from .validation import validate_plan


def _snapshot(path: str) -> OpenProjectSnapshot:
    return OpenProjectSnapshot.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _operation_json(operation: object) -> dict[str, object]:
    return dict(vars(operation))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="planning")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("backlog")
    graph = commands.add_parser("graph")
    graph.add_argument("backlog")
    graph.add_argument("--format", choices=("mermaid",), default="mermaid")
    diff = commands.add_parser("diff")
    diff.add_argument("backlog")
    diff.add_argument("--against-openproject", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("backlog")
    publish.add_argument("--against-openproject", required=True)
    mode = publish.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    reconcile_parser = commands.add_parser("reconcile")
    reconcile_parser.add_argument("backlog")
    reconcile_parser.add_argument("--against-openproject", required=True)
    reconcile_parser.add_argument("--plan", required=True, metavar="PLAN_ID")
    reconcile_parser.add_argument("--approved-commit", metavar="MERGE_SHA")
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.backlog)
    except (OSError, SchemaValidationError) as error:
        print(f"invalid schema: {error}")
        return 2
    issues = validate_plan(plan)
    if args.command == "validate":
        for issue in issues:
            print(f"{issue.code}: {issue.node_key or '-'}: {issue.message}")
        return 1 if issues else 0
    if issues:
        for issue in issues:
            print(f"{issue.code}: {issue.node_key or '-'}: {issue.message}")
        return 2
    if args.command == "graph":
        print(render_mermaid(plan), end="")
        return 0
    snapshot = _snapshot(args.against_openproject)
    if args.command == "diff":
        print(
            json.dumps(
                [_operation_json(operation) for operation in plan_diff(plan, snapshot)],
                indent=2,
                default=list,
            )
        )
        return 0
    if args.command == "publish":
        if args.apply:
            print(
                "apply requires an injected PublicationAdapter; CLI only supports --dry-run",
                flush=True,
            )
            return 2
        print(
            json.dumps(
                [_operation_json(operation) for operation in plan_diff(plan, snapshot)],
                indent=2,
                default=list,
            )
        )
        return 0
    if args.plan != plan.plan.id:
        print("--plan must match the loaded artifact plan id")
        return 2
    report = reconcile(plan, snapshot, approved_commit=args.approved_commit)
    print(
        json.dumps(
            {
                "findings": [vars(finding) for finding in report.findings],
                "safe_repairs": [_operation_json(operation) for operation in report.safe_repairs],
            },
            indent=2,
            default=list,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
