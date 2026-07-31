from __future__ import annotations

import ast
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parents[1]
ROOT = REPO_ROOT / "windmill"
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
    "alertmanager_webhook",
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
    for source in ("github", "openproject", "alertmanager"):
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
        expected_script = (
            "alertmanager_delivery" if source == "alertmanager" else "verify_webhook"
        )
        assert expected_script in str(value["modules"][0])
        if source == "alertmanager":
            assert trigger["request_type"] == "sync"
            failure = value["failure_module"]
            assert isinstance(failure, dict)
            assert failure["value"]["path"] == "f/planning/alert_delivery_failure"

    failure_source = (PLANNING / "alert_delivery_failure.py").read_text()
    assert '"windmill_status_code": 503' in failure_source
    assert 'UUID(_required("WM_JOB_ID"))' in failure_source
    assert "operational_alert_payload_sha256" in failure_source
    delivery_source = (PLANNING / "alertmanager_delivery.py").read_text()
    assert "operational_alert_is_stale" in delivery_source
    assert 'UUID(_required("WM_JOB_ID"))' in delivery_source
    assert "operational_alert_payload_sha256" in delivery_source
    assert "pg_advisory_xact_lock" in delivery_source

    schedule = _document(PLANNING / "nightly_reconciliation.schedule.yaml")
    assert schedule["enabled"] is True
    assert schedule["is_flow"] is True
    assert schedule["timezone"] == "America/Los_Angeles"


def test_release_image_contains_the_complete_workspace_snapshot() -> None:
    workspace_files = sorted(
        path.relative_to(ROOT)
        for path in ROOT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    assert len(workspace_files) == 37

    dockerignore = (REPO_ROOT / ".dockerignore").read_text().splitlines()
    assert "!windmill/" in dockerignore
    assert "!windmill/**" in dockerignore
    assert "windmill/**/__pycache__/" in dockerignore
    assert "windmill/**/*.pyc" in dockerignore

    dockerfile = (REPO_ROOT / "deploy/windmill/extend.Dockerfile").read_text()
    assert "COPY windmill/ /opt/planning-platform-workspace/" in dockerfile
    assert "ARG NPM_VERSION=11.19.0" in dockerfile
    assert 'npm install --global "npm@${NPM_VERSION}"' in dockerfile
    assert 'tar/package.json").version' in dockerfile
    assert "rm -f /usr/bin/wmill" in dockerfile
    assert 'npm install --global "windmill-cli@1.775.2"' in dockerfile
    assert 'wmill_version="$(wmill --version)"' in dockerfile
    assert 'grep -Fx "CLI version: 1.775.2" >/dev/null' in dockerfile
    assert 'grep -Fqx "CLI version: 1.775.2"' not in dockerfile
    assert "bun install -g windmill-cli" not in dockerfile
    assert "/root/.bun/install/cache" not in dockerfile
    assert "ENV PYTHONPATH=/opt/planning-platform" in dockerfile
    assert "ADDITIONAL_PYTHON_PATHS=/opt/planning-platform" in dockerfile

    workflow = (REPO_ROOT / ".github/workflows/release-images.yml").read_text()
    assert "docker run --rm --platform linux/amd64 --user 1000:1000" in workflow
    assert "--entrypoint wmill" in workflow
    assert "grep -Fx 'CLI version: 1.775.2' >/dev/null" in workflow
    assert "grep -Fqx 'CLI version: 1.775.2'" not in workflow
