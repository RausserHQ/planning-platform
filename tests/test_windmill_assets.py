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
    "convergence_check",
    "nightly_reconciliation",
    "replan_affected_subgraph",
    "dead_letter_recovery",
    "alertmanager_webhook",
}
ASYNC_BACKED_SCRIPTS = {
    "abandon_terminal_resume",
    "convergence_check",
    "dead_letter",
    "lifecycle_job",
    "nightly_reconciliation",
    "publish_openproject_graph",
    "recover_dead_letter",
    "replan_affected_subgraph",
}
BAKED_DEPENDENCY_LOCK = (
    "# py: 3.12\n# Dependencies are provided by the pinned planning-platform Windmill image.\n"
)


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
        lock = source.with_suffix(".script.lock")
        assert lock.read_text() == BAKED_DEPENDENCY_LOCK, source
        assert _document(metadata)["lock"] == (f"!inline f/planning/{source.stem}.script.lock"), (
            source
        )

        source_text = source.read_text()
        tree = ast.parse(source_text)
        entrypoints = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
        ]
        assert len(entrypoints) == 1, source
        assert isinstance(entrypoints[0], ast.FunctionDef), source
        async_helpers = [
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_main_async"
        ]
        assert bool(async_helpers) is (source.stem in ASYNC_BACKED_SCRIPTS), source
        if async_helpers:
            assert "return asyncio.run(_main_async(" in source_text, source


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
        assert script["lock"] == BAKED_DEPENDENCY_LOCK
        assert "def preprocessor(event)" in str(script["content"])
        expected_script = "alertmanager_delivery" if source == "alertmanager" else "verify_webhook"
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
    assert "discover_openproject_adapter(" in delivery_source
    assert '_required("OPENPROJECT_CANONICAL_ORIGIN")' in delivery_source

    verifier_source = (PLANNING / "verify_webhook.py").read_text()
    assert "openproject_client(" in verifier_source
    assert 'os.environ.get("OPENPROJECT_CANONICAL_ORIGIN"' in verifier_source

    schedule = _document(PLANNING / "nightly_reconciliation.schedule.yaml")
    assert schedule["enabled"] is True
    assert schedule["is_flow"] is True
    assert schedule["timezone"] == "America/Los_Angeles"


def test_convergence_proof_is_operator_only_and_uses_a_stable_windmill_identity() -> None:
    flow = _document(PLANNING / "convergence_check.flow/flow.yaml")
    assert flow["schema"]["required"] == ["plan_id", "plan_version"]
    modules = flow["value"]["modules"]
    assert [module["value"]["path"] for module in modules] == [
        "f/planning/convergence_event",
        "f/planning/convergence_check",
    ]
    assert modules[1]["retry"]["retry_if"] == {"expr": 'error.name != "ManualFailure"'}
    assert flow["value"]["failure_module"]["value"]["input_transforms"]["event"] == {
        "type": "javascript",
        "expr": "results.envelope ?? {}",
    }
    assert flow["value"]["failure_module"]["value"]["input_transforms"]["preserve_failure"] == {
        "type": "static",
        "value": True,
    }
    assert not (PLANNING / "convergence_check.http_trigger.yaml").exists()
    assert not (PLANNING / "convergence_check.schedule.yaml").exists()

    source = (PLANNING / "convergence_event.py").read_text()
    metadata = _document(PLANNING / "convergence_event.script.yaml")
    assert set(metadata["schema"]["properties"]) == {"plan_id", "plan_version"}
    tree = ast.parse(source)
    entrypoint = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
    )
    assert [argument.arg for argument in entrypoint.args.args] == ["plan_id", "plan_version"]
    assert "WM_ROOT_FLOW_JOB_ID" in source
    assert "WM_JOB_ID" in source
    assert "delivery_id or" not in source
    assert "convergence_check_envelope" in source

    check_source = (PLANNING / "convergence_check.py").read_text()
    check_metadata = _document(PLANNING / "convergence_check.script.yaml")
    assert set(check_metadata["schema"]["properties"]) == {"event"}
    assert '"wm_failure": result.outcome' in check_source
    assert '"wm_failure": "convergence_result_unavailable"' in check_source
    assert "convergence envelope is not bound to this Windmill root job" in check_source
    assert "execute_delivery" in check_source

    dead_letter_source = (PLANNING / "dead_letter.py").read_text()
    assert '"wm_failure": "convergence_check_failed"' in dead_letter_source


def test_partial_replan_is_operator_only_and_root_job_bound() -> None:
    flow = _document(PLANNING / "replan_affected_subgraph.flow/flow.yaml")
    assert flow["schema"]["required"] == [
        "plan_id",
        "base_plan_version",
        "affected_node_keys",
        "reason",
    ]
    assert [module["value"]["path"] for module in flow["value"]["modules"]] == [
        "f/planning/replan_event",
        "f/planning/replan_affected_subgraph",
    ]
    assert flow["value"]["modules"][1]["value"]["input_transforms"]["event"] == {
        "type": "javascript",
        "expr": "results.envelope",
    }
    assert "WM_ROOT_FLOW_JOB_ID" in (PLANNING / "replan_event.py").read_text()
    assert "WM_JOB_ID" in (PLANNING / "replan_affected_subgraph.py").read_text()


def test_release_image_contains_the_complete_workspace_snapshot() -> None:
    workspace_files = sorted(
        path.relative_to(ROOT)
        for path in ROOT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    assert len(workspace_files) == 61

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
    assert (
        'windmill_python="$(uv python find 3.12 --system '
        '--python-preference only-managed)"'
    ) in dockerfile
    cache_create = (
        'build_uv_cache="$(mktemp -d /tmp/planning-platform-build-uv.XXXXXX)"'
    )
    cache_export = 'export UV_CACHE_DIR="${build_uv_cache}"'
    cache_remove = 'rm -rf "${build_uv_cache:?}"'
    assert cache_create in dockerfile
    assert cache_export in dockerfile
    assert cache_remove in dockerfile
    assert (
        dockerfile.index(cache_create)
        < dockerfile.index(cache_export)
        < dockerfile.index('windmill_python="$(uv python find')
        < dockerfile.index(cache_remove)
    )
    assert 'uv pip install --python "${windmill_python}" --require-hashes' in dockerfile
    assert 'uv pip install --python "${windmill_python}" --no-deps' in dockerfile
    assert 'pq.__impl__ == "binary"' in dockerfile
    assert 'npm install --global "windmill-cli@1.775.2"' in dockerfile
    assert 'wmill_version="$(wmill --version)"' in dockerfile
    assert 'grep -Fx "CLI version: 1.775.2" >/dev/null' in dockerfile
    assert 'grep -Fqx "CLI version: 1.775.2"' not in dockerfile
    assert "bun install -g windmill-cli" not in dockerfile
    assert "/root/.bun/install/cache" not in dockerfile
    assert "ENV PYTHONPATH=/opt/planning-platform" in dockerfile
    assert "ADDITIONAL_PYTHON_PATHS=/opt/planning-platform" in dockerfile

    workflow = (REPO_ROOT / ".github/workflows/release-images.yml").read_text()
    assert "WINDMILL_BASE: ghcr.io/windmill-labs/windmill:1.775.2@sha256:" in workflow
    assert "Verify official Windmill CE base identity" in workflow
    assert "WINDMILL_BASE=${{ env.WINDMILL_BASE }}" in workflow
    assert "context: upstream" not in workflow
    assert "docker run --rm --platform linux/amd64 --user 1000:1000" in workflow
    assert "tests/verify_windmill_entrypoints.py" in workflow
    assert "windmill-smoke-uv" not in workflow
    assert 'export UV_CACHE_DIR="$uv_cache"' not in workflow
    assert "--workspace /opt/planning-platform-workspace --expected-python 3.12" in workflow
    assert 'exec "$windmill_python" /tmp/verify_windmill_entrypoints.py' in workflow
    assert "--workdir /opt/planning-platform-workspace" in workflow
    assert "--entrypoint wmill" in workflow
    assert 'wmill "${image}" lint --locks-required' in workflow
    assert "grep -Fx 'CLI version: 1.775.2' >/dev/null" in workflow
    assert "grep -Fqx 'CLI version: 1.775.2'" not in workflow
