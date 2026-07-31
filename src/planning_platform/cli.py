"""Small local-only CLI for deterministic planning artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml  # type: ignore[import-untyped]

from .diff import plan_diff
from .graph import render_mermaid
from .loader import LoadedArtifact, SchemaValidationError, load_artifact
from .openproject import OpenProjectSnapshot
from .openproject_adapter import (
    OpenProjectAdapterConfig,
    OpenProjectPublicationAdapter,
    openproject_target_sha256,
)
from .publication_journal import PostgresPublicationJournal
from .publisher import PublicationEnvelope
from .publisher import publish as publish_artifact
from .reconciliation import reconcile
from .validation import validate_plan
from .yaml_loader import load_unique_yaml


class CliInputError(ValueError):
    """A safe, non-secret-bearing command configuration error."""


_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_TRUSTED_REF = re.compile(r"^refs/(?:heads|remotes)/[A-Za-z0-9][A-Za-z0-9._/-]*$")


@dataclass(frozen=True)
class PublicationPolicy:
    repository_root: Path
    expected_origin_url: str
    trusted_ref: str
    artifact_path: PurePosixPath
    openproject_target_sha256: str


def _snapshot(path: str) -> OpenProjectSnapshot:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("OpenProject snapshot must be an object")
    try:
        return OpenProjectSnapshot.from_dict(value)
    except (AttributeError, KeyError, OverflowError, TypeError) as error:
        raise ValueError("OpenProject snapshot has an invalid structure") from error


def _operation_json(operation: object) -> dict[str, object]:
    return dict(vars(operation))


def _operation_summary(operation: object) -> dict[str, object]:
    value = vars(operation)
    return {
        "operation_id": value["operation_id"],
        "kind": value["kind"],
        "identity": value["identity"],
    }


def _mapping_document(path: str, label: str) -> dict[str, Any]:
    try:
        value = load_unique_yaml(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise CliInputError(f"{label} could not be loaded") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CliInputError(f"{label} must be a mapping with string keys")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise CliInputError(f"{label} must be a positive integer")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CliInputError(f"{label} must be a positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise CliInputError(f"{label} must be a finite positive number")
    return number


def _id_mapping(value: object, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CliInputError(f"{label} must map names to positive integer IDs")
    return {key: _integer(identifier, f"{label}.{key}") for key, identifier in value.items()}


def _publisher_config(path: str) -> OpenProjectAdapterConfig:
    value = _mapping_document(path, "publisher config")
    required = {
        "base_url",
        "project_id",
        "alert_assignee_id",
        "type_ids",
        "status_ids",
        "custom_field_ids",
        "priority_ids",
    }
    optional = {
        "timeout_seconds",
        "page_size",
        "max_collection_pages",
        "max_collection_items",
    }
    if set(value) - required - optional:
        raise CliInputError("publisher config contains unsupported keys")
    if required - set(value):
        raise CliInputError("publisher config is missing required keys")
    base_url = value["base_url"]
    if not isinstance(base_url, str) or not base_url.strip():
        raise CliInputError("publisher config base_url must be a non-empty string")
    try:
        return OpenProjectAdapterConfig(
            base_url=base_url,
            project_id=_integer(value["project_id"], "publisher config project_id"),
            alert_assignee_id=_integer(
                value["alert_assignee_id"], "publisher config alert_assignee_id"
            ),
            type_ids=_id_mapping(value["type_ids"], "publisher config type_ids"),
            status_ids=_id_mapping(value["status_ids"], "publisher config status_ids"),
            custom_field_ids=_id_mapping(
                value["custom_field_ids"], "publisher config custom_field_ids"
            ),
            priority_ids=_id_mapping(
                value["priority_ids"], "publisher config priority_ids"
            ),
            timeout_seconds=_number(
                value.get("timeout_seconds", 10.0), "publisher config timeout_seconds"
            ),
            page_size=_integer(value.get("page_size", 100), "publisher config page_size"),
            max_collection_pages=_integer(
                value.get("max_collection_pages", 100),
                "publisher config max_collection_pages",
            ),
            max_collection_items=_integer(
                value.get("max_collection_items", 10_000),
                "publisher config max_collection_items",
            ),
        )
    except ValueError as error:
        raise CliInputError(str(error)) from error


def _publication_envelope(
    path: str, *, publication_target_sha256: str
) -> PublicationEnvelope:
    value = _mapping_document(path, "publication envelope")
    fields = {
        "approved_commit",
        "backlog_sha256",
        "artifact_blob_sha1",
        "approval_event_id",
        "snapshot_sha256",
        "snapshot_etag",
        "trace_id",
        "publication_identity",
    }
    if set(value) != fields:
        raise CliInputError("publication envelope must contain exactly the required fields")
    if not all(isinstance(value[field], str) and value[field] for field in fields):
        raise CliInputError("publication envelope fields must be non-empty strings")
    return PublicationEnvelope(
        **{field: value[field] for field in fields},
        publication_target_sha256=publication_target_sha256,
    )


def _publication_policy(path: str) -> PublicationPolicy:
    value = _mapping_document(path, "publication policy")
    fields = {
        "repository_root",
        "expected_origin_url",
        "trusted_ref",
        "artifact_path",
        "openproject_target_sha256",
    }
    if set(value) != fields:
        raise CliInputError("publication policy must contain exactly the required fields")
    if not all(isinstance(value[field], str) and value[field] for field in fields):
        raise CliInputError("publication policy fields must be non-empty strings")
    root_input = Path(value["repository_root"])
    if not root_input.is_absolute():
        raise CliInputError("publication policy repository_root must be absolute")
    try:
        root = root_input.resolve(strict=True)
    except OSError as error:
        raise CliInputError("publication policy repository_root is unavailable") from error
    if not root.is_dir():
        raise CliInputError("publication policy repository_root must be a directory")
    artifact_text = value["artifact_path"]
    artifact_path = PurePosixPath(artifact_text)
    if (
        artifact_path.is_absolute()
        or artifact_path.as_posix() != artifact_text
        or not artifact_path.parts
        or any(part in {"", ".", ".."} for part in artifact_path.parts)
    ):
        raise CliInputError("publication policy artifact_path must be a normalized relative path")
    trusted_ref = value["trusted_ref"]
    if (
        _TRUSTED_REF.fullmatch(trusted_ref) is None
        or ".." in trusted_ref
        or "//" in trusted_ref
        or trusted_ref.endswith(("/", ".", ".lock"))
    ):
        raise CliInputError("publication policy trusted_ref is invalid")
    target = value["openproject_target_sha256"]
    if re.fullmatch(r"[0-9a-f]{64}", target) is None:
        raise CliInputError("publication policy OpenProject target hash is invalid")
    origin = value["expected_origin_url"]
    if origin != origin.strip() or any(character in origin for character in "\r\n\0"):
        raise CliInputError("publication policy expected_origin_url is invalid")
    return PublicationPolicy(root, origin, trusted_ref, artifact_path, target)


def _git(repository_root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(repository_root), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CliInputError("publication policy Git verification failed") from error
    return result


def _git_output(repository_root: Path, *arguments: str) -> bytes:
    result = _git(repository_root, *arguments)
    if result.returncode != 0:
        raise CliInputError("publication policy Git verification failed")
    return result.stdout


def _verify_git_binding(
    artifact: LoadedArtifact,
    envelope: PublicationEnvelope,
    backlog_path: str,
    policy: PublicationPolicy,
) -> None:
    if _COMMIT_SHA.fullmatch(envelope.approved_commit) is None:
        raise CliInputError("publication envelope approved_commit is invalid")
    root_output = _git_output(policy.repository_root, "rev-parse", "--show-toplevel")
    try:
        observed_root = Path(root_output.decode("utf-8").strip()).resolve(strict=True)
    except (OSError, UnicodeError) as error:
        raise CliInputError("publication policy Git repository identity is invalid") from error
    if observed_root != policy.repository_root:
        raise CliInputError("publication policy Git repository root changed")
    origin = _git_output(policy.repository_root, "remote", "get-url", "origin")
    try:
        observed_origin = origin.decode("utf-8").rstrip("\r\n")
    except UnicodeError as error:
        raise CliInputError("publication policy Git origin identity is invalid") from error
    if observed_origin != policy.expected_origin_url:
        raise CliInputError("publication policy Git origin identity changed")
    resolved_commit = _git_output(
        policy.repository_root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{envelope.approved_commit}^{{commit}}",
    ).strip()
    if resolved_commit != envelope.approved_commit.encode("ascii"):
        raise CliInputError("publication envelope approved_commit is not an exact Git commit")
    _git_output(
        policy.repository_root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{policy.trusted_ref}^{{commit}}",
    )
    ancestry = _git(
        policy.repository_root,
        "merge-base",
        "--is-ancestor",
        envelope.approved_commit,
        policy.trusted_ref,
    )
    if ancestry.returncode != 0:
        raise CliInputError("approved commit is not reachable from the trusted Git ref")
    expected_file = policy.repository_root.joinpath(*policy.artifact_path.parts)
    current = policy.repository_root
    for part in policy.artifact_path.parts:
        current /= part
        try:
            file_mode = current.lstat().st_mode
        except OSError as error:
            raise CliInputError("publication policy artifact path is unavailable") from error
        if stat.S_ISLNK(file_mode):
            raise CliInputError("publication policy artifact path cannot contain symlinks")
    if not stat.S_ISREG(expected_file.lstat().st_mode):
        raise CliInputError("publication policy artifact path must be a regular file")
    supplied_path = Path(os.path.abspath(backlog_path))
    if supplied_path != expected_file:
        raise CliInputError("backlog path does not match the publication policy artifact path")
    tree_entry = _git_output(
        policy.repository_root,
        "ls-tree",
        "-z",
        envelope.approved_commit,
        "--",
        policy.artifact_path.as_posix(),
    )
    if tree_entry.count(b"\0") != 1 or not tree_entry.endswith(b"\0"):
        raise CliInputError("approved commit has no unique backlog blob")
    try:
        metadata, committed_path = tree_entry[:-1].split(b"\t", 1)
        git_mode, object_type, blob_sha1 = metadata.split(b" ", 2)
        path_text = committed_path.decode("utf-8")
        blob_text = blob_sha1.decode("ascii")
    except (ValueError, UnicodeError) as error:
        raise CliInputError("approved commit backlog tree entry is malformed") from error
    if (
        git_mode not in {b"100644", b"100755"}
        or object_type != b"blob"
        or path_text != policy.artifact_path.as_posix()
        or _COMMIT_SHA.fullmatch(blob_text) is None
    ):
        raise CliInputError("approved commit backlog is not a regular Git blob")
    committed_bytes = _git_output(policy.repository_root, "cat-file", "blob", blob_text)
    if (
        blob_text != artifact.blob_sha1
        or blob_text != envelope.artifact_blob_sha1
        or committed_bytes != artifact.raw_bytes
        or hashlib.sha256(committed_bytes).hexdigest() != artifact.sha256
        or artifact.sha256 != envelope.backlog_sha256
    ):
        raise CliInputError("backlog bytes do not match the approved Git commit")


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise CliInputError(f"{name} is required")
    return value


def _path_option(value: str | None, environment: str, option: str) -> str:
    resolved = value or os.environ.get(environment)
    if resolved is None or not resolved.strip():
        raise CliInputError(f"{option} or {environment} is required")
    return resolved


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
    publish.add_argument("--against-openproject")
    publish.add_argument("--publisher-config")
    publish.add_argument("--publication-envelope")
    mode = publish.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    publication_target = commands.add_parser("publication-target")
    publication_target.add_argument("--publisher-config")
    reconcile_parser = commands.add_parser("reconcile")
    reconcile_parser.add_argument("backlog")
    reconcile_parser.add_argument("--against-openproject", required=True)
    reconcile_parser.add_argument("--plan", required=True, metavar="PLAN_ID")
    reconcile_parser.add_argument("--approved-commit", metavar="MERGE_SHA")
    args = parser.parse_args(argv)
    if args.command == "publication-target":
        try:
            config_path = _path_option(
                args.publisher_config,
                "PLANNING_OPENPROJECT_PUBLISHER_CONFIG",
                "--publisher-config",
            )
            print(openproject_target_sha256(_publisher_config(config_path)))
            return 0
        except CliInputError as error:
            print(f"publication configuration rejected: {error}")
            return 2
    try:
        artifact = load_artifact(args.backlog)
    except SchemaValidationError:
        print("planning artifact failed schema validation")
        return 2
    except (OSError, UnicodeError, yaml.YAMLError, ValueError):
        print("planning artifact could not be loaded")
        return 2
    plan = artifact.plan
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
    if args.command == "diff":
        try:
            snapshot = _snapshot(args.against_openproject)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            print("OpenProject snapshot could not be loaded")
            return 2
        print(
            json.dumps(
                [_operation_json(operation) for operation in plan_diff(plan, snapshot)],
                indent=2,
                default=list,
            )
        )
        return 0
    if args.command == "publish":
        if args.dry_run:
            if not args.against_openproject:
                print("--against-openproject is required with --dry-run")
                return 2
            if args.publisher_config or args.publication_envelope:
                print("live publisher options cannot be used with --dry-run")
                return 2
            try:
                snapshot = _snapshot(args.against_openproject)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                print("OpenProject snapshot could not be loaded")
                return 2
            print(
                json.dumps(
                    [_operation_json(operation) for operation in plan_diff(plan, snapshot)],
                    indent=2,
                    default=list,
                )
            )
            return 0
        if args.against_openproject:
            print("--against-openproject cannot be used with --apply; live state is required")
            return 2
        try:
            config_path = _path_option(
                args.publisher_config,
                "PLANNING_OPENPROJECT_PUBLISHER_CONFIG",
                "--publisher-config",
            )
            envelope_path = _path_option(
                args.publication_envelope,
                "PLANNING_PUBLICATION_ENVELOPE",
                "--publication-envelope",
            )
            config = _publisher_config(config_path)
            policy = _publication_policy(_required_environment("PLANNING_PUBLICATION_POLICY"))
            target_sha256 = openproject_target_sha256(config)
            if target_sha256 != policy.openproject_target_sha256:
                raise CliInputError("publisher config does not match publication policy")
            envelope = _publication_envelope(
                envelope_path,
                publication_target_sha256=target_sha256,
            )
            _verify_git_binding(artifact, envelope, args.backlog, policy)
            token = _required_environment("OPENPROJECT_API_TOKEN")
            database_url = _required_environment("PLANNING_LIFECYCLE_DATABASE_URL")
            with OpenProjectPublicationAdapter(config, token) as adapter:
                result = publish_artifact(
                    artifact,
                    adapter,
                    envelope,
                    apply=True,
                    journal=PostgresPublicationJournal(database_url),
                )
        except CliInputError as error:
            print(f"publication configuration rejected: {error}")
            return 2
        except Exception as error:
            # Adapter and journal failures can wrap transport/database details.
            # Their raw text is deliberately withheld so credentials embedded
            # in a URL or a third-party exception cannot reach command output.
            print(f"publication failed: {type(error).__name__}")
            return 2
        print(
            json.dumps(
                {
                    "applied": result.applied,
                    "resumed": result.resumed,
                    "operation_count": len(result.applied_operations),
                    "operations": [
                        _operation_summary(operation)
                        for operation in result.applied_operations
                    ],
                },
                indent=2,
                default=list,
            )
        )
        return 0
    try:
        snapshot = _snapshot(args.against_openproject)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        print("OpenProject snapshot could not be loaded")
        return 2
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
