from pathlib import Path

import pytest

from evals.scoring import score_plan
from planning_platform.loader import load_plan
from planning_platform.models import AcceptanceCriterion, AgentEligibility, ResultPredicate
from planning_platform.validation import validate_plan


def test_representative_fixture_scores_all_quality_dimensions() -> None:
    fixture = Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    score = score_plan(load_plan(fixture), expected_requirements={"REQ-1"})
    assert score.verticality == 1
    assert score.boundedness == 1
    assert score.dependency_integrity == 1
    assert score.observability == 1
    assert score.command_safety == 1
    assert score.coverage == 1
    assert score.duplication == 1
    assert score.unnecessary_serialization == 1
    assert score.overall == 1


def test_unsafe_unobservable_fake_traceability_plan_scores_poorly() -> None:
    fixture = Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    plan = load_plan(fixture)
    unsafe = plan.items[0].model_copy(
        update={
            "title": "Replace the entire platform",
            "objective": "Replace everything in one indivisible change.",
            "risk": "critical",
            "agent_eligibility": AgentEligibility(
                eligible=True, reason="Execute without human review."
            ),
            "source_requirements": ("REQ-FAKE",),
            "acceptance_criteria": (
                AcceptanceCriterion(
                    criterion="Everything works.",
                    observation="It looks good after a manual review.",
                ),
            ),
            "result_predicate": ResultPredicate(kind="command", expression="success"),
        }
    )
    changed = plan.model_copy(update={"items": (unsafe,)})
    score = score_plan(changed, expected_requirements={"REQ-1"})
    assert score.verticality == 0
    assert score.boundedness == 0
    assert score.observability == 0
    assert score.command_safety == 0
    assert score.coverage == 0
    assert score.overall < 0.6


@pytest.mark.parametrize(
    "command",
    (
        "rm -rf /",
        "sudo python -m pytest -q",
        "kubectl delete namespace production",
        "kubectl --context production -n system delete deployment api",
        "curl https://example.test -X PATCH -d '{\"enabled\":true}'",
        "wget --method=DELETE https://example.test/resource",
        "wget --method DELETE https://example.test/resource",
        "tofu apply -auto-approve",
        "tofu fmt -check=false -recursive",
        "terraform -chdir=infrastructure destroy -auto-approve",
        "bash -lc 'kubectl --context production delete pod api'",
        "find /tmp -delete",
        "python -c \"import os; os.remove('/tmp/unsafe')\"",
        "perl -e 'unlink q{/tmp/unsafe}'",
        "ruby -e 'File.delete %q{/tmp/unsafe}'",
        "pytest -q && find /tmp -delete",
        "unknown-validator check",
        "ruff check --fix .",
        "pyright --createstub generated",
        "go test -exec /tmp/unknown-wrapper ./...",
        "go test -exec=/tmp/unknown-wrapper ./...",
        "go vet -vettool /tmp/unknown-vet ./...",
        "go vet -vettool=/tmp/unknown-vet ./...",
        "go test -toolexec=/tmp/unknown-tool ./...",
        "/tmp/pytest -q",
        "./pytest -q",
        "ruff check --output-file report.txt .",
        "ruff check --output-file=report.txt .",
        "pytest -p malicious_plugin -q",
        "pytest --junitxml=report.xml -q",
        "pytest --basetemp=/tmp/pytest-output -q",
        "mypy --config-file malicious.ini src",
        "mypy --junit-xml report.xml src",
        "helm template chart --post-renderer malicious-renderer",
        "helm template chart --output-dir rendered",
        "helm lint chart --dependency-update",
        "cargo clippy --fix",
        "cargo test --config target.runner=malicious",
        "go test -coverprofile=coverage.out ./...",
        "go test -cpuprofile=cpu.out ./...",
        "git --paginate diff --check",
        "git -c core.pager=cat diff --check",
        "git --config-env=core.pager=PAGER diff --check",
        "git --exec-path=/tmp diff --check",
        "git diff --ext-diff",
        "git diff --textconv",
        "git grep --open-files-in-pager=vim pattern",
        "pytest /etc/passwd -q",
        "pytest tests/../../etc/passwd -q",
        "pytest ../outside/test_module.py -q",
        "pytest '~/outside/test_module.py' -q",
        r"pytest 'C:\outside\test_module.py' -q",
        r"pytest '\\server\share\test_module.py' -q",
        "pytest file:///etc/passwd -q",
        "pytest https://example.test/test_module.py -q",
        "pytest @/tmp/attacker.args -q",
        "pytest @../outside.args -q",
        r"pytest '@C:\outside\attacker.args' -q",
        "pytest @~/attacker.args -q",
        "pytest @workspace/pytest.args -q",
        "ruff check --exclude=key=/etc/passwd .",
        r"ruff check '--exclude=key=C:\outside' .",
        "ruff check --config pyproject.toml .",
        "terraform -chdir=../outside validate",
        "kubectl --kubeconfig workspace/kubeconfig get pods",
        "helm template chart --set config.path=/etc/passwd",
        'helm template chart --set-json \'config={"path":"/etc/passwd"}\'',
        "helm template chart --set-string config.path=../outside",
        "helm template chart --set-file config.path=workspace/value",
        "helm template chart --values workspace/values.yaml",
        "helm template chart -f workspace/values.yaml",
        "cargo test --manifest-path workspace/Cargo.toml",
        "git diff --no-index workspace/file /etc/passwd",
        "git diff --relative=../outside",
        "yamllint --config-file workspace/yamllint.yaml .",
    ),
)
def test_otherwise_valid_plan_with_destructive_command_cannot_score_perfectly(
    command: str,
) -> None:
    fixture = Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    plan = load_plan(fixture)
    unsafe = plan.items[0].model_copy(
        update={
            "validation_commands": (command,),
            "result_predicate": ResultPredicate(
                kind="command",
                expression=f"{command} exits 0",
            ),
        }
    )
    score = score_plan(
        plan.model_copy(update={"items": (unsafe,)}),
        expected_requirements={"REQ-1"},
    )
    assert score.command_safety == 0
    assert score.observability == 0
    assert score.overall < 1


@pytest.mark.parametrize(
    "command",
    (
        "pytest -q",
        "python -m pytest -q",
        "ruff check .",
        "tofu fmt -check -recursive",
        "terraform -chdir=infrastructure validate",
        "kubectl --context production -n system get deployments",
        "git diff --check",
    ),
)
def test_known_read_only_validation_commands_are_allowed(command: str) -> None:
    fixture = Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    plan = load_plan(fixture)
    item = plan.items[0].model_copy(
        update={
            "validation_commands": (command,),
            "result_predicate": ResultPredicate(
                kind="command",
                expression=f"{command} exits 0",
            ),
        }
    )
    score = score_plan(
        plan.model_copy(update={"items": (item,)}),
        expected_requirements={"REQ-1"},
    )
    assert score.command_safety == 1


def test_relative_path_is_only_syntax_safe_until_runtime_resolves_symlinks() -> None:
    fixture = Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    plan = load_plan(fixture)
    command = "pytest workspace-link/test_module.py -q"
    item = plan.items[0].model_copy(
        update={
            "validation_commands": (command,),
            "result_predicate": ResultPredicate(
                kind="command",
                expression=f"{command} exits 0",
            ),
        }
    )
    score = score_plan(
        plan.model_copy(update={"items": (item,)}),
        expected_requirements={"REQ-1"},
    )
    assert score.command_safety == 1


def test_coverage_fails_closed_without_expected_requirement_set() -> None:
    fixture = Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    score = score_plan(load_plan(fixture))
    assert score.coverage == 0
    assert score.overall < 1


def test_maintenance_objectives_do_not_substitute_for_source_requirements() -> None:
    fixture = Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    plan = load_plan(fixture)
    maintenance_only = plan.items[0].model_copy(
        update={
            "source_requirements": (),
            "maintenance_objectives": ("REQ-1",),
        }
    )
    score = score_plan(
        plan.model_copy(update={"items": (maintenance_only,)}),
        expected_requirements={"REQ-1"},
    )
    assert score.coverage == 0


def test_coverage_fails_when_valid_and_unexpected_requirements_are_mixed() -> None:
    fixture = Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    plan = load_plan(fixture)
    mixed = plan.items[0].model_copy(update={"source_requirements": ("REQ-1", "REQ-FABRICATED")})
    score = score_plan(
        plan.model_copy(update={"items": (mixed,)}),
        expected_requirements={"REQ-1"},
    )
    assert score.coverage == 0


@pytest.mark.parametrize("hierarchy_type", ("Epic", "Initiative"))
def test_hierarchy_items_cannot_hide_unexpected_requirements(
    hierarchy_type: str,
) -> None:
    fixture = Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    plan = load_plan(fixture)
    hierarchy = plan.items[0].model_copy(
        update={
            "key": f"fabricated-{hierarchy_type.casefold()}",
            "type": hierarchy_type,
            "title": f"Fabricated {hierarchy_type} trace",
            "objective": "Group valid work while citing an undeclared requirement.",
            "source_requirements": ("REQ-FABRICATED",),
        }
    )
    changed = plan.model_copy(update={"items": (hierarchy, *plan.items)})
    assert not validate_plan(changed)
    score = score_plan(changed, expected_requirements={"REQ-1"})
    assert score.coverage == 0
    assert score.overall < 1


def test_any_semantic_failure_zeroes_dependency_integrity() -> None:
    fixture = Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    plan = load_plan(fixture)
    conflicting = plan.items[0].model_copy(
        update={
            "key": "verify-core",
            "title": "Verify deterministic core",
            "blocked_by": (plan.items[0].key,),
            "related_to": (plan.items[0].key,),
        }
    )
    score = score_plan(
        plan.model_copy(update={"items": (*plan.items, conflicting)}),
        expected_requirements={"REQ-1"},
    )
    assert score.dependency_integrity == 0


def test_hard_blockers_count_as_serialization_structure() -> None:
    fixture = Path(__file__).parents[2] / "evals/fixtures/single-repository/backlog.yaml"
    plan = load_plan(fixture)
    second = plan.items[0].model_copy(
        update={
            "key": "verify-core",
            "title": "Verify deterministic core",
            "blocked_by": (plan.items[0].key,),
        }
    )
    score = score_plan(
        plan.model_copy(update={"items": (*plan.items, second)}),
        expected_requirements={"REQ-1"},
    )
    assert score.unnecessary_serialization == 0
