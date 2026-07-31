from __future__ import annotations

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1] / "windmill"
PLANNING = ROOT / "f/planning"
REQUIRED_FLOWS = {
    "idea_created",
    "idea_moved_to_planning",
    "planning_input_received",
    "planning_resume",
    "planning_artifacts_ready",
    "planning_pr_merged",
    "publish_openproject_graph",
    "openproject_work_package_changed",
    "github_pull_request_event",
    "github_check_run_event",
    "nightly_reconciliation",
    "replan_affected_subgraph",
    "dead_letter_recovery",
}


def _document(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text())
    assert isinstance(value, dict), path
    return value


def test_windmill_workspace_has_every_versioned_flow_and_executable_script() -> None:
    config = _document(ROOT / "wmill.yaml")
    assert config["includeSchedules"] is True
    assert config["includeTriggers"] is True
    assert config["skipSecrets"] is True

    flows = {path.parent.name.removesuffix(".flow") for path in PLANNING.glob("*.flow/flow.yaml")}
    assert flows >= REQUIRED_FLOWS
    for source in PLANNING.glob("*.py"):
        metadata = source.with_suffix(".script.yaml")
        assert metadata.is_file(), source
        tree = ast.parse(source.read_text())
        assert any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
            for node in tree.body
        ), source


def test_flow_modules_resolve_and_retries_use_supported_bounded_shape() -> None:
    for path in PLANNING.glob("*.flow/flow.yaml"):
        flow = _document(path)
        value = flow["value"]
        assert isinstance(value, dict)
        modules = value["modules"]
        assert isinstance(modules, list) and modules
        special = [
            value.get("preprocessor_module"),
            value.get("failure_module"),
        ]
        for module in [*modules, *(entry for entry in special if entry is not None)]:
            assert isinstance(module, dict)
            module_value = module["value"]
            assert isinstance(module_value, dict)
            assert isinstance(module_value.get("input_transforms"), dict)
            if module_value.get("type") == "script":
                script = str(module_value["path"]).removeprefix("f/planning/")
                assert (PLANNING / f"{script}.py").is_file(), (path, script)
                assert (PLANNING / f"{script}.script.yaml").is_file(), (path, script)
            retry = module.get("retry")
            if retry is None:
                continue
            assert isinstance(retry, dict)
            assert not {"attempts", "backoff_seconds", "dead_letter"} & retry.keys()
            attempts = 0
            for policy_name in ("constant", "exponential"):
                policy = retry.get(policy_name)
                if policy is not None:
                    assert isinstance(policy, dict)
                    attempts += int(policy["attempts"])
                    assert int(policy["seconds"]) >= 1
            assert 1 <= attempts <= 4


def test_webhook_triggers_preserve_raw_body_and_enter_a_v2_preprocessor() -> None:
    for source in ("github", "openproject"):
        trigger = _document(PLANNING / f"{source}_webhook.http_trigger.yaml")
        assert trigger["authentication_method"] == "none"
        assert trigger["http_method"] == "post"
        assert trigger["raw_string"] is True
        assert trigger["is_flow"] is True
        flow = _document(PLANNING / f"{source}_webhook.flow/flow.yaml")
        value = flow["value"]
        assert isinstance(value, dict)
        preprocessor = value["preprocessor_module"]
        assert isinstance(preprocessor, dict)
        script = preprocessor["value"]
        assert isinstance(script, dict)
        assert script["language"] == "python3"
        assert "def preprocessor(event)" in str(script["content"])
        assert "verify_webhook" in str(value["modules"][0])

    schedule = _document(PLANNING / "nightly_reconciliation.schedule.yaml")
    assert schedule["enabled"] is True
    assert schedule["is_flow"] is True
    assert schedule["timezone"] == "America/Los_Angeles"
