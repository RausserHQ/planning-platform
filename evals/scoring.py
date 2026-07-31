"""Adversarial decomposition-quality scoring for fixture and model evaluation."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from planning_platform.models import BacklogPlan
from planning_platform.validation import validate_plan

_OBSERVABLE = re.compile(
    r"(?i)(exit(?:s| code)?\s*(?:is|=)?\s*(?:zero|0)|status\s*(?:is|=)?\s*\d{3}|"
    r"metric|exists|equals|returns|below|above|<|>|check run)"
)
_VAGUE = re.compile(r"(?i)\b(manual|looks good|works|done|success|as expected|tbd)\b")
_OVERBROAD = re.compile(
    r"(?i)\b(everything|all systems|entire platform|every subsystem|one indivisible)\b"
)
_SHELL_CONTROL = re.compile(r"[\n\r;&|`<>$()]")
_PYTHON_EXECUTABLE = re.compile(r"python(?:\d+(?:\.\d+)*)?")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)^[a-z]:[\\/]")
_URI_PATH = re.compile(r"(?i)^[a-z][a-z0-9+.-]*://")
_KUBECTL_READ_ACTIONS = {
    "api-resources",
    "api-versions",
    "cluster-info",
    "describe",
    "diff",
    "explain",
    "get",
    "logs",
    "options",
    "top",
    "version",
    "wait",
}
_GIT_READ_ACTIONS = {"diff", "grep", "log", "ls-files", "rev-parse", "show", "status"}
_OBSERVATION_SUFFIXES = (
    ("returns", "exit", "code", "zero"),
    ("returns", "exit", "code", "0"),
    ("exit", "code", "zero"),
    ("exit", "code", "0"),
    ("exits", "zero"),
    ("exits", "0"),
)


@dataclass(frozen=True)
class EvaluationScore:
    verticality: float
    boundedness: float
    dependency_integrity: float
    observability: float
    command_safety: float
    coverage: float
    duplication: float
    unnecessary_serialization: float

    @property
    def overall(self) -> float:
        return (
            sum(
                (
                    self.verticality,
                    self.boundedness,
                    self.dependency_integrity,
                    self.observability,
                    self.command_safety,
                    self.coverage,
                    self.duplication,
                    self.unnecessary_serialization,
                )
            )
            / 8
        )


def score_plan(
    plan: BacklogPlan,
    *,
    expected_requirements: set[str] | None = None,
) -> EvaluationScore:
    executable = [
        item for item in plan.items if item.type in {"Story", "Task", "Investigation", "Bug"}
    ]
    denominator = max(1, len(executable))
    vertical = sum(
        bool(item.acceptance_criteria)
        and not _OVERBROAD.search(f"{item.title} {item.objective}")
        and any(_OBSERVABLE.search(criterion.observation) for criterion in item.acceptance_criteria)
        for item in executable
    )
    bounded = sum(
        not _OVERBROAD.search(f"{item.title} {item.objective}")
        and not (item.risk in {"high", "critical"} and item.agent_eligibility.eligible)
        for item in executable
    )
    command_safe = {
        item.key: not any(
            not _command_is_safe(command)
            for command in (
                *item.validation_commands,
                *(
                    (item.result_predicate.expression,)
                    if item.result_predicate.kind == "command"
                    else ()
                ),
            )
        )
        for item in executable
    }
    observable = sum(
        all(
            _OBSERVABLE.search(criterion.observation) and not _VAGUE.search(criterion.observation)
            for criterion in item.acceptance_criteria
        )
        and bool(item.required_evidence)
        and bool(_OBSERVABLE.search(item.result_predicate.expression))
        and command_safe[item.key]
        for item in executable
    )
    executable_traces = {trace for item in executable for trace in item.source_requirements}
    all_source_traces = {trace for item in plan.items for trace in item.source_requirements}
    if expected_requirements is None:
        coverage = 0.0
    else:
        expected = expected_requirements
        if all_source_traces - expected or any(
            not (set(item.source_requirements) & expected) for item in executable
        ):
            coverage = 0
        else:
            coverage = len(executable_traces & expected) / max(1, len(expected))
    titles = [item.title.casefold().strip() for item in plan.items]
    duplicate_quality = len(set(titles)) / max(1, len(titles))
    ordering_edges = sum(len(item.sequence_after) + len(item.blocked_by) for item in plan.items)
    possible_edges = max(1, len(plan.items) * (len(plan.items) - 1) // 2)
    integrity = not validate_plan(plan)
    return EvaluationScore(
        verticality=vertical / denominator,
        boundedness=bounded / denominator,
        dependency_integrity=float(integrity),
        observability=observable / denominator,
        command_safety=sum(command_safe.values()) / denominator,
        coverage=min(1.0, coverage),
        duplication=duplicate_quality,
        unnecessary_serialization=max(0.0, 1 - ordering_edges / possible_edges),
    )


def _command_is_safe(command: str) -> bool:
    if not command.strip() or _SHELL_CONTROL.search(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    tokens = _without_observation_suffix(tokens)
    if not tokens:
        return False
    if "/" in tokens[0] or "\\" in tokens[0]:
        return False
    executable = tokens[0].casefold()
    arguments = tokens[1:]
    if _PYTHON_EXECUTABLE.fullmatch(executable):
        if len(arguments) < 2 or arguments[0] != "-m":
            return False
        if "/" in arguments[1] or "\\" in arguments[1]:
            return False
        executable = arguments[1].casefold()
        arguments = arguments[2:]
    return _allowed_validation_tool(executable, arguments)


def _without_observation_suffix(tokens: list[str]) -> list[str]:
    lowered = tuple(token.casefold() for token in tokens)
    for suffix in _OBSERVATION_SUFFIXES:
        if lowered[-len(suffix) :] == suffix:
            return tokens[: -len(suffix)]
    return tokens


def _allowed_validation_tool(executable: str, arguments: list[str]) -> bool:
    lowered = [argument.casefold() for argument in arguments]
    if executable in {"pytest", "py.test"}:
        return _pytest_arguments_are_safe(arguments)
    if executable == "mypy":
        return _arguments_are_allowlisted(
            lowered,
            flags={
                "--check-untyped-defs",
                "--disallow-untyped-defs",
                "--no-error-summary",
                "--pretty",
                "--show-error-codes",
                "--strict",
                "--version",
                "--warn-unused-ignores",
            },
            value_options={"--exclude", "--platform", "--python-version"},
        )
    if executable == "pyright":
        return _arguments_are_allowlisted(
            lowered,
            flags={"--stats", "--verbose", "--version", "--warnings"},
            value_options={"--level", "--pythonplatform", "--pythonversion"},
        )
    if executable == "shellcheck":
        return _arguments_are_allowlisted(
            lowered,
            flags={"--check-sourced", "--external-sources", "--norc", "-a", "-x"},
            value_options={
                "--color",
                "--exclude",
                "--format",
                "--severity",
                "--shell",
                "-e",
                "-f",
                "-s",
                "-S",
            },
        )
    if executable == "yamllint":
        return _arguments_are_allowlisted(
            lowered,
            flags={"--no-warnings", "--strict", "-s"},
            value_options={"--config-data", "--format", "-d", "-f"},
        )
    if executable == "ruff":
        if not lowered:
            return False
        action, tail = lowered[0], lowered[1:]
        common_flags = {"--isolated", "--no-cache", "--preview", "--quiet", "--silent", "--verbose"}
        common_values = {
            "--exclude",
            "--extend-exclude",
            "--target-version",
        }
        if action == "check":
            return _arguments_are_allowlisted(
                tail,
                flags=common_flags
                | {
                    "--diff",
                    "--exit-non-zero-on-fix",
                    "--exit-zero",
                    "--no-fix",
                    "--no-unsafe-fixes",
                    "--show-files",
                    "--show-settings",
                    "--statistics",
                },
                value_options=common_values
                | {
                    "--extend-ignore",
                    "--extend-select",
                    "--ignore",
                    "--output-format",
                    "--select",
                },
            )
        return (
            action == "format"
            and "--check" in tail
            and _arguments_are_allowlisted(
                tail,
                flags=common_flags | {"--check", "--diff"},
                value_options=common_values
                | {
                    "--line-length",
                    "--range",
                },
            )
        )
    if executable == "kubectl":
        parsed = _action_after_global_options(
            lowered,
            flags={
                "--disable-compression",
                "--insecure-skip-tls-verify",
                "--match-server-version",
                "--warnings-as-errors",
            },
            options_with_values={
                "--as",
                "--as-group",
                "--cluster",
                "--context",
                "--namespace",
                "--request-timeout",
                "--server",
                "--user",
                "--v",
                "--vmodule",
                "-n",
                "-s",
            },
        )
        if parsed is None or parsed[0] not in _KUBECTL_READ_ACTIONS:
            return False
        return _arguments_are_allowlisted(
            parsed[1],
            flags={
                "--all-namespaces",
                "--ignore-not-found",
                "--prefix",
                "--previous",
                "--server-side",
                "--show-labels",
                "--show-managed-fields",
                "--timestamps",
                "--watch",
                "-a",
                "-p",
                "-w",
            },
            value_options={
                "--chunk-size",
                "--container",
                "--field-manager",
                "--field-selector",
                "--limit-bytes",
                "--output",
                "--selector",
                "--since",
                "--since-time",
                "--sort-by",
                "--tail",
                "-c",
                "-l",
                "-o",
            },
        )
    if executable in {"tofu", "terraform"}:
        parsed = _action_after_global_options(
            lowered,
            flags=set(),
            options_with_values={"-chdir"},
        )
        if parsed is None:
            return False
        action, tail = parsed
        if action == "fmt":
            return any(
                argument in {"-check", "--check", "-check=true", "--check=true"}
                for argument in tail
            ) and _arguments_are_allowlisted(
                tail,
                flags={
                    "--check",
                    "--check=true",
                    "-check",
                    "-check=true",
                    "-diff",
                    "-list",
                    "-no-color",
                    "-recursive",
                    "-write=false",
                },
            )
        if action == "validate":
            return _arguments_are_allowlisted(
                tail,
                flags={"-json", "-no-color"},
                value_options={"-test-directory"},
            )
        return action == "version" and _arguments_are_allowlisted(
            tail,
            flags={"-json"},
            allow_positionals=False,
        )
    if executable == "git":
        if any(argument == "-c" or argument.startswith("-c=") for argument in lowered):
            return False
        parsed = _action_after_global_options(
            lowered,
            flags={"--no-pager"},
            options_with_values=set(),
        )
        if parsed is None or parsed[0] not in _GIT_READ_ACTIONS:
            return False
        return _arguments_are_allowlisted(
            parsed[1],
            flags={
                "--branch",
                "--cached",
                "--check",
                "--exit-code",
                "--is-inside-work-tree",
                "--line-number",
                "--name-only",
                "--name-status",
                "--no-ext-diff",
                "--no-patch",
                "--no-textconv",
                "--oneline",
                "--patch",
                "--porcelain",
                "--quiet",
                "--short",
                "--show-toplevel",
                "--staged",
                "--stat",
                "--untracked",
                "-b",
                "-i",
                "-n",
                "-p",
                "-s",
            },
            value_options={
                "--diff-filter",
                "--format",
                "--max-count",
                "--regexp",
                "--relative",
                "--unified",
                "--untracked-files",
                "-e",
            },
        )
    if executable == "helm":
        if not lowered or lowered[0] not in {"lint", "template", "version"}:
            return False
        return _arguments_are_allowlisted(
            lowered[1:],
            flags={"--debug", "--quiet", "--strict", "--with-subcharts"},
            value_options={
                "--api-versions",
                "--kube-version",
                "--name-template",
                "--namespace",
                "--set",
                "--set-json",
                "--set-string",
            },
        )
    if executable == "go":
        if not lowered:
            return False
        action, tail = lowered[0], lowered[1:]
        if action == "version":
            return not tail
        if action == "vet":
            return _arguments_are_allowlisted(
                tail,
                flags={"-json", "-v"},
                value_options={"-tags"},
            )
        return action == "test" and _arguments_are_allowlisted(
            tail,
            flags={"-cover", "-failfast", "-json", "-race", "-short", "-v"},
            value_options={
                "-bench",
                "-benchtime",
                "-count",
                "-parallel",
                "-run",
                "-tags",
                "-timeout",
            },
        )
    if executable == "cargo":
        if not lowered:
            return False
        action, tail = lowered[0], lowered[1:]
        common_flags = {
            "--all-features",
            "--all-targets",
            "--frozen",
            "--locked",
            "--no-default-features",
            "--offline",
            "--quiet",
            "--verbose",
            "--workspace",
            "-q",
            "-v",
        }
        common_values = {
            "--features",
            "--jobs",
            "--package",
            "-j",
            "-p",
        }
        if action in {"check", "clippy", "test"}:
            return _arguments_are_allowlisted(
                tail,
                flags=common_flags,
                value_options=common_values,
            )
        return (
            action == "fmt"
            and "--check" in tail
            and _arguments_are_allowlisted(
                tail,
                flags={"--all", "--check", "--quiet", "--verbose", "-q", "-v"},
                value_options={"--package", "-p"},
            )
        )
    return False


def _action_after_global_options(
    arguments: list[str],
    *,
    flags: set[str],
    options_with_values: set[str],
) -> tuple[str, list[str]] | None:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if not argument.startswith("-"):
            return argument, arguments[index + 1 :]
        if argument in flags:
            index += 1
            continue
        option, separator, value = argument.partition("=")
        if option not in options_with_values:
            return None
        if separator:
            if not value or not _workspace_value_is_syntax_safe(value):
                return None
            index += 1
        else:
            if (
                index + 1 >= len(arguments)
                or arguments[index + 1].startswith("-")
                or not _workspace_value_is_syntax_safe(arguments[index + 1])
            ):
                return None
            index += 2
    return None


def _arguments_are_allowlisted(
    arguments: list[str],
    *,
    flags: set[str],
    value_options: set[str] | None = None,
    allow_positionals: bool = True,
) -> bool:
    options_with_values = value_options or set()
    expecting_value = False
    for argument in arguments:
        if expecting_value:
            if argument.startswith("-") or not _workspace_value_is_syntax_safe(argument):
                return False
            expecting_value = False
            continue
        if not argument.startswith("-"):
            if not allow_positionals or not _workspace_value_is_syntax_safe(argument):
                return False
            continue
        if argument in flags:
            if "=" in argument and not _workspace_value_is_syntax_safe(argument.split("=", 1)[1]):
                return False
            continue
        option, separator, value = argument.partition("=")
        if option not in options_with_values:
            return False
        if separator:
            if not value or not _workspace_value_is_syntax_safe(value):
                return False
        else:
            expecting_value = True
    return not expecting_value


def _workspace_value_is_syntax_safe(value: str) -> bool:
    """Reject values that lexically escape a workspace; symlinks require runtime checks."""
    candidates = [value, *re.split(r"[=,:,\s]+", value)]
    for candidate in candidates:
        candidate = candidate.strip(" \t\r\n'\"{}[]")
        if not candidate:
            continue
        if candidate.startswith(("/", "\\", "~", "@")):
            return False
        if _WINDOWS_ABSOLUTE_PATH.match(candidate):
            return False
        if _URI_PATH.match(candidate) or candidate.casefold().startswith("file:"):
            return False
        if ".." in re.split(r"[/\\]", candidate):
            return False
    return True


def _pytest_arguments_are_safe(arguments: list[str]) -> bool:
    safe_flags = {
        "-q",
        "--quiet",
        "-v",
        "-vv",
        "--verbose",
        "-x",
        "--exitfirst",
        "-s",
        "--collect-only",
        "--co",
        "--disable-warnings",
        "--strict-markers",
        "--strict-config",
        "--no-header",
        "--no-summary",
    }
    return _arguments_are_allowlisted(
        arguments,
        flags=safe_flags,
        value_options={"-k", "-m", "--capture", "--color", "--maxfail", "--tb"},
    )
