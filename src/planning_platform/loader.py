"""Strict YAML loading backed by the versioned JSON schema."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

from .models import BacklogPlan


class SchemaValidationError(ValueError):
    """A YAML document does not conform to the frozen JSON schema."""


@dataclass(frozen=True)
class LoadedArtifact:
    """The raw planning artifact bound into an immutable publication envelope."""

    plan: BacklogPlan
    raw_bytes: bytes
    sha256: str
    blob_sha1: str


def schema_path() -> Path:
    return (
        Path(__file__).resolve().parents[2] / "packages/backlog-schema/schema/backlog.schema.json"
    )


def _raw_mapping(raw_bytes: bytes) -> dict[str, Any]:
    data = yaml.safe_load(raw_bytes.decode("utf-8"))
    if not isinstance(data, dict):
        raise SchemaValidationError("backlog document must be a mapping")
    return data


def load_raw(path: str | Path) -> dict[str, Any]:
    return _raw_mapping(Path(path).read_bytes())


def validate_schema(data: dict[str, Any]) -> None:
    schema = json.loads(schema_path().read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(data), key=str
    )
    if errors:
        details = "; ".join(
            f"{'/'.join(map(str, error.path)) or '$'}: {error.message}" for error in errors
        )
        raise SchemaValidationError(details)


def load_plan(path: str | Path) -> BacklogPlan:
    return load_artifact(path).plan


def load_artifact(path: str | Path) -> LoadedArtifact:
    raw_bytes = Path(path).read_bytes()
    data = _raw_mapping(raw_bytes)
    validate_schema(data)
    return LoadedArtifact(
        plan=BacklogPlan.model_validate(data),
        raw_bytes=raw_bytes,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        blob_sha1=hashlib.sha1(f"blob {len(raw_bytes)}\0".encode("ascii") + raw_bytes).hexdigest(),
    )
