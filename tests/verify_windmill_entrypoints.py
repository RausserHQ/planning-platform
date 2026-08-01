#!/usr/bin/env python3
"""Smoke-test Windmill script imports and synchronous async-backed entrypoints."""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

EXPECTED_SCRIPT_COUNT = 12
EXPECTED_ASYNC_BACKED_COUNT = 7
REQUIRED_ARGUMENTS: dict[str, Any] = {
    "event": {},
    "operator": "release-smoke-test",
    "reason": "verify synchronous Windmill entrypoint",
}


def _load_module(path: Path) -> ModuleType:
    name = f"_planning_platform_windmill_smoke_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Windmill script: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _required_kwargs(signature: inspect.Signature) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if parameter.default is not inspect.Parameter.empty:
            continue
        if name not in REQUIRED_ARGUMENTS:
            raise RuntimeError(f"no smoke-test value for required parameter: {name}")
        values[name] = REQUIRED_ARGUMENTS[name]
    return values


def _verify_async_backed_entrypoint(module: ModuleType, path: Path) -> None:
    entrypoint = module.main
    helper = module._main_async
    entrypoint_signature = inspect.signature(entrypoint)
    helper_signature = inspect.signature(helper)
    if entrypoint_signature != helper_signature:
        raise RuntimeError(f"entrypoint signature differs from async helper: {path}")

    invocation = _required_kwargs(entrypoint_signature)
    expected = helper_signature.bind(**invocation)
    expected.apply_defaults()
    captured: dict[str, inspect.BoundArguments] = {}
    sentinel = object()

    async def stub(*args: Any, **kwargs: Any) -> object:
        bound = helper_signature.bind(*args, **kwargs)
        bound.apply_defaults()
        captured["arguments"] = bound
        return sentinel

    module._main_async = stub
    result = entrypoint(**invocation)
    if result is not sentinel:
        raise RuntimeError(f"entrypoint did not return the awaited helper result: {path}")
    if captured.get("arguments") is None:
        raise RuntimeError(f"entrypoint did not execute the async helper: {path}")
    if captured["arguments"].arguments != expected.arguments:
        raise RuntimeError(f"entrypoint changed async helper arguments: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--expected-python", default="3.12")
    args = parser.parse_args()

    actual_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual_python != args.expected_python:
        raise RuntimeError(f"expected Python {args.expected_python}, found Python {actual_python}")

    scripts = sorted((args.workspace / "f/planning").glob("*.py"))
    if len(scripts) != EXPECTED_SCRIPT_COUNT:
        raise RuntimeError(
            f"expected {EXPECTED_SCRIPT_COUNT} Windmill scripts, found {len(scripts)}"
        )

    async_backed: list[Path] = []
    for path in scripts:
        module = _load_module(path)
        entrypoint = getattr(module, "main", None)
        if not callable(entrypoint):
            raise RuntimeError(f"missing callable main entrypoint: {path}")
        if inspect.iscoroutinefunction(entrypoint):
            raise RuntimeError(f"Windmill entrypoint must be synchronous: {path}")
        helper = getattr(module, "_main_async", None)
        if helper is None:
            continue
        if not inspect.iscoroutinefunction(helper):
            raise RuntimeError(f"_main_async must be a coroutine function: {path}")
        _verify_async_backed_entrypoint(module, path)
        async_backed.append(path)

    if len(async_backed) != EXPECTED_ASYNC_BACKED_COUNT:
        raise RuntimeError(
            "expected "
            f"{EXPECTED_ASYNC_BACKED_COUNT} async-backed scripts, found {len(async_backed)}"
        )
    print(
        "verified "
        f"{len(scripts)} imports and {len(async_backed)} synchronous async-backed entrypoints"
    )


if __name__ == "__main__":
    main()
