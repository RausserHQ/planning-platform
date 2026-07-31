from __future__ import annotations

import json
import subprocess
from pathlib import Path

from planning_platform.cli import _git_output, _publisher_config, main
from planning_platform.graph import render_mermaid
from planning_platform.loader import load_artifact, load_plan
from planning_platform.openproject_adapter import openproject_target_sha256
from planning_platform.publisher import PublishResult

FIXTURE = Path(__file__).parents[1] / "evals/fixtures/single-repository/backlog.yaml"
PUBLISHER_CONFIG = Path(__file__).parents[1] / "openproject/publisher-config.example.yaml"


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _apply_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "approved-repository"
    repo.mkdir()
    backlog = repo / "backlog.yaml"
    backlog.write_bytes(FIXTURE.read_bytes())
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.name", "Planning CLI Test")
    _git(repo, "config", "user.email", "planning-cli@example.invalid")
    _git(
        repo,
        "remote",
        "add",
        "origin",
        "https://github.com/RausserHQ/planning-platform.git",
    )
    _git(repo, "add", "--", "backlog.yaml")
    _git(repo, "commit", "-q", "-m", "Approve planning artifact")
    approved_commit = _git(repo, "rev-parse", "HEAD")
    artifact = load_artifact(backlog)
    envelope = tmp_path / "envelope.json"
    envelope.write_text(
        json.dumps(
            {
                "approved_commit": approved_commit,
                "backlog_sha256": artifact.sha256,
                "artifact_blob_sha1": artifact.blob_sha1,
                "approval_event_id": "merged-planning-pr:fixture",
                "snapshot_sha256": artifact.plan.plan.openproject_snapshot.sha256,
                "snapshot_etag": artifact.plan.plan.openproject_snapshot.etag,
                "trace_id": "fixture-trace",
                "publication_identity": artifact.plan.plan.publication_identity,
            }
        ),
        encoding="utf-8",
    )
    policy = tmp_path / "publication-policy.json"
    policy.write_text(
        json.dumps(
            {
                "repository_root": str(repo),
                "expected_origin_url": "https://github.com/RausserHQ/planning-platform.git",
                "trusted_ref": "refs/heads/main",
                "artifact_path": "backlog.yaml",
                "openproject_target_sha256": openproject_target_sha256(
                    _publisher_config(str(PUBLISHER_CONFIG))
                ),
            }
        ),
        encoding="utf-8",
    )
    return backlog, envelope, policy


def test_graph_and_validate_cli(capsys) -> None:
    plan = load_plan(FIXTURE)
    assert "flowchart TD" in render_mermaid(plan)
    assert main(["validate", str(FIXTURE)]) == 0
    assert main(["graph", str(FIXTURE), "--format", "mermaid"]) == 0
    assert "implement-core" in capsys.readouterr().out


def test_publication_target_cli_prints_canonical_nonsecret_hash(capsys) -> None:
    assert (
        main(
            [
                "publication-target",
                "--publisher-config",
                str(PUBLISHER_CONFIG),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out.strip()
    assert output == openproject_target_sha256(_publisher_config(str(PUBLISHER_CONFIG)))
    assert len(output) == 64


def test_diff_and_dry_run_cli(tmp_path: Path, capsys) -> None:
    plan = load_plan(FIXTURE)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "captured_at": "now",
                "etag": "fixture",
                "sha256": plan.plan.openproject_snapshot.sha256,
                "work_packages": [],
            }
        )
    )
    assert main(["diff", str(FIXTURE), "--against-openproject", str(snapshot)]) == 0
    assert "create_work_package" in capsys.readouterr().out
    assert main(["publish", str(FIXTURE), "--against-openproject", str(snapshot), "--dry-run"]) == 0


def test_snapshot_commands_reject_malformed_structure(tmp_path: Path, capsys) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("{}", encoding="utf-8")

    assert main(["diff", str(FIXTURE), "--against-openproject", str(snapshot)]) == 2
    assert capsys.readouterr().out == "OpenProject snapshot could not be loaded\n"


def test_git_verification_ignores_repository_environment_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    approved_root = tmp_path / "approved"
    approved_root.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(approved_root)],
        check=True,
        capture_output=True,
    )
    _git(approved_root, "config", "user.name", "Planning CLI Test")
    _git(approved_root, "config", "user.email", "planning-cli@example.invalid")
    (approved_root / "artifact").write_text("approved", encoding="utf-8")
    _git(approved_root, "add", "artifact")
    _git(approved_root, "commit", "-q", "-m", "approved")
    approved_commit = _git(approved_root, "rev-parse", "HEAD")

    attacker_root = tmp_path / "attacker"
    attacker_root.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(attacker_root)],
        check=True,
        capture_output=True,
    )
    _git(attacker_root, "config", "user.name", "Planning CLI Test")
    _git(attacker_root, "config", "user.email", "planning-cli@example.invalid")
    (attacker_root / "artifact").write_text("attacker", encoding="utf-8")
    _git(attacker_root, "add", "artifact")
    _git(attacker_root, "commit", "-q", "-m", "attacker")
    assert _git(attacker_root, "rev-parse", "HEAD") != approved_commit

    monkeypatch.setenv("GIT_DIR", str(attacker_root / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(attacker_root))
    observed = _git_output(approved_root, "rev-parse", "HEAD").decode().strip()

    assert observed == approved_commit


def test_reconcile_cli_requires_matching_plan_id(tmp_path: Path) -> None:
    plan = load_plan(FIXTURE)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "captured_at": "now",
                "etag": "fixture",
                "sha256": plan.plan.openproject_snapshot.sha256,
                "work_packages": [],
            }
        )
    )
    command = [
        "reconcile",
        str(FIXTURE),
        "--against-openproject",
        str(snapshot),
        "--plan",
        plan.plan.id,
        "--approved-commit",
        "a" * 40,
    ]
    assert main(command) == 0
    command[-3] = "wrong-plan"
    assert main(command) == 2


def test_apply_cli_wires_live_adapter_and_durable_journal(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    backlog, envelope, policy = _apply_files(tmp_path)
    artifact = load_artifact(backlog)
    observed: dict[str, object] = {}
    token = "openproject-token-must-stay-secret"
    database_url = "postgresql://publisher:database-password@postgres/planning"

    class FakeAdapter:
        def __init__(self, config, supplied_token: str) -> None:
            observed["config"] = config
            observed["token"] = supplied_token

        def __enter__(self):
            observed["entered"] = True
            return self

        def __exit__(self, *_: object) -> None:
            observed["closed"] = True

    class FakeJournal:
        def __init__(self, supplied_database_url: str) -> None:
            observed["database_url"] = supplied_database_url

    def fake_publish(loaded, adapter, immutable_envelope, *, apply, journal):
        observed["artifact"] = loaded
        observed["adapter"] = adapter
        observed["envelope"] = immutable_envelope
        observed["apply"] = apply
        observed["journal"] = journal
        return PublishResult(operations=(), applied=True)

    monkeypatch.setattr("planning_platform.cli.OpenProjectPublicationAdapter", FakeAdapter)
    monkeypatch.setattr("planning_platform.cli.PostgresPublicationJournal", FakeJournal)
    monkeypatch.setattr("planning_platform.cli.publish_artifact", fake_publish)
    monkeypatch.setenv("OPENPROJECT_API_TOKEN", token)
    monkeypatch.setenv("PLANNING_LIFECYCLE_DATABASE_URL", database_url)
    monkeypatch.setenv("PLANNING_PUBLICATION_POLICY", str(policy))

    assert (
        main(
            [
                "publish",
                str(backlog),
                "--apply",
                "--publisher-config",
                str(PUBLISHER_CONFIG),
                "--publication-envelope",
                str(envelope),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "applied": True,
        "resumed": False,
        "operation_count": 0,
        "operations": [],
    }
    assert token not in output
    assert "database-password" not in output
    assert observed["artifact"] == artifact
    assert observed["token"] == token
    assert observed["database_url"] == database_url
    assert observed["apply"] is True
    assert observed["entered"] is True
    assert observed["closed"] is True


def test_apply_cli_requires_environment_credentials(tmp_path: Path, monkeypatch, capsys) -> None:
    backlog, envelope, policy = _apply_files(tmp_path)
    monkeypatch.delenv("OPENPROJECT_API_TOKEN", raising=False)
    monkeypatch.setenv(
        "PLANNING_LIFECYCLE_DATABASE_URL",
        "postgresql://publisher:database-password@postgres/planning",
    )
    monkeypatch.setenv("PLANNING_PUBLICATION_POLICY", str(policy))

    assert (
        main(
            [
                "publish",
                str(backlog),
                "--apply",
                "--publisher-config",
                str(PUBLISHER_CONFIG),
                "--publication-envelope",
                str(envelope),
            ]
        )
        == 2
    )
    output = capsys.readouterr().out
    assert "OPENPROJECT_API_TOKEN is required" in output
    assert "database-password" not in output


def test_apply_cli_withholds_secret_bearing_runtime_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    backlog, envelope, policy = _apply_files(tmp_path)
    token = "openproject-token-must-stay-secret"
    database_url = "postgresql://publisher:database-password@postgres/planning"

    class FakeAdapter:
        def __init__(self, config, supplied_token: str) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

    class FakeJournal:
        def __init__(self, supplied_database_url: str) -> None:
            pass

    def failed_publish(*args, **kwargs):
        raise RuntimeError(f"unsafe upstream detail: {token} {database_url}")

    monkeypatch.setattr("planning_platform.cli.OpenProjectPublicationAdapter", FakeAdapter)
    monkeypatch.setattr("planning_platform.cli.PostgresPublicationJournal", FakeJournal)
    monkeypatch.setattr("planning_platform.cli.publish_artifact", failed_publish)
    monkeypatch.setenv("OPENPROJECT_API_TOKEN", token)
    monkeypatch.setenv("PLANNING_LIFECYCLE_DATABASE_URL", database_url)
    monkeypatch.setenv("PLANNING_PUBLICATION_POLICY", str(policy))

    assert (
        main(
            [
                "publish",
                str(backlog),
                "--apply",
                "--publisher-config",
                str(PUBLISHER_CONFIG),
                "--publication-envelope",
                str(envelope),
            ]
        )
        == 2
    )
    output = capsys.readouterr().out
    assert "publication failed: RuntimeError" in output
    assert token not in output
    assert "database-password" not in output


def test_apply_cli_rejects_duplicate_config_and_envelope_keys(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    backlog, envelope, policy = _apply_files(tmp_path)
    duplicate_config = tmp_path / "duplicate-config.yaml"
    duplicate_config.write_text(
        PUBLISHER_CONFIG.read_text(encoding="utf-8")
        + "\nbase_url: https://credential-redirect.example.invalid\n",
        encoding="utf-8",
    )
    duplicate_envelope = tmp_path / "duplicate-envelope.yaml"
    envelope_text = envelope.read_text(encoding="utf-8")
    duplicate_envelope.write_text(
        envelope_text.replace(
            '"trace_id": "fixture-trace"',
            '"trace_id": "fixture-trace", "trace_id": "replacement-trace"',
        ),
        encoding="utf-8",
    )
    token = "openproject-token-must-stay-secret"
    database_url = "postgresql://publisher:database-password@postgres/planning"
    monkeypatch.setenv("OPENPROJECT_API_TOKEN", token)
    monkeypatch.setenv("PLANNING_LIFECYCLE_DATABASE_URL", database_url)
    monkeypatch.setenv("PLANNING_PUBLICATION_POLICY", str(policy))

    def forbidden_adapter(*args, **kwargs):
        raise AssertionError("adapter must not receive credentials for ambiguous YAML")

    monkeypatch.setattr(
        "planning_platform.cli.OpenProjectPublicationAdapter", forbidden_adapter
    )

    for config, immutable_envelope in (
        (duplicate_config, envelope),
        (PUBLISHER_CONFIG, duplicate_envelope),
    ):
        assert (
            main(
                [
                    "publish",
                    str(backlog),
                    "--apply",
                    "--publisher-config",
                    str(config),
                    "--publication-envelope",
                    str(immutable_envelope),
                ]
            )
            == 2
        )
        output = capsys.readouterr().out
        assert "could not be loaded" in output
        assert "credential-redirect" not in output
        assert token not in output
        assert "database-password" not in output


def test_cli_withholds_malformed_or_schema_invalid_artifact_content(
    tmp_path: Path, capsys
) -> None:
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("plan: [\n  SECRET_MALFORMED_VALUE\n", encoding="utf-8")
    assert main(["validate", str(malformed)]) == 2
    output = capsys.readouterr().out
    assert "could not be loaded" in output
    assert "SECRET_MALFORMED_VALUE" not in output

    invalid = tmp_path / "schema-invalid.yaml"
    invalid.write_bytes(FIXTURE.read_bytes() + b"\nsecret_payload: SECRET_SCHEMA_VALUE\n")
    assert main(["validate", str(invalid)]) == 2
    output = capsys.readouterr().out
    assert "failed schema validation" in output
    assert "SECRET_SCHEMA_VALUE" not in output

    duplicate = tmp_path / "duplicate-artifact.yaml"
    duplicate.write_text(
        FIXTURE.read_text(encoding="utf-8").replace(
            "    title: Implement deterministic core",
            "    title: Reviewed title\n    title: SECRET_DUPLICATE_OVERRIDE",
            1,
        ),
        encoding="utf-8",
    )
    assert main(["validate", str(duplicate)]) == 2
    output = capsys.readouterr().out
    assert "could not be loaded" in output
    assert "SECRET_DUPLICATE_OVERRIDE" not in output


def test_apply_cli_rejects_nonfinite_timeout_before_credentials(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    backlog, envelope, _ = _apply_files(tmp_path)
    monkeypatch.setenv("OPENPROJECT_API_TOKEN", "must-not-be-used")
    for value in (".inf", ".nan"):
        config = tmp_path / f"nonfinite-{value.removeprefix('.')}.yaml"
        config.write_text(
            PUBLISHER_CONFIG.read_text(encoding="utf-8").replace(
                "timeout_seconds: 10", f"timeout_seconds: {value}"
            ),
            encoding="utf-8",
        )
        assert (
            main(
                [
                    "publish",
                    str(backlog),
                    "--apply",
                    "--publisher-config",
                    str(config),
                    "--publication-envelope",
                    str(envelope),
                ]
            )
            == 2
        )
        output = capsys.readouterr().out
        assert "finite positive number" in output
        assert "must-not-be-used" not in output


def test_apply_cli_rejects_uncommitted_artifact_bytes_before_credentials(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    backlog, envelope, policy = _apply_files(tmp_path)
    backlog.write_bytes(backlog.read_bytes() + b"\n# unapproved local change\n")
    monkeypatch.setenv("PLANNING_PUBLICATION_POLICY", str(policy))
    monkeypatch.setenv("OPENPROJECT_API_TOKEN", "must-not-be-used")

    def forbidden_adapter(*args, **kwargs):
        raise AssertionError("adapter must not receive credentials for unapproved bytes")

    monkeypatch.setattr(
        "planning_platform.cli.OpenProjectPublicationAdapter", forbidden_adapter
    )
    assert (
        main(
            [
                "publish",
                str(backlog),
                "--apply",
                "--publisher-config",
                str(PUBLISHER_CONFIG),
                "--publication-envelope",
                str(envelope),
            ]
        )
        == 2
    )
    output = capsys.readouterr().out
    assert "approved Git commit" in output
    assert "must-not-be-used" not in output


def test_apply_cli_rejects_commit_outside_trusted_ref(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    backlog, envelope, policy = _apply_files(tmp_path)
    repo = backlog.parent
    _git(repo, "checkout", "-q", "--orphan", "untrusted")
    backlog.write_bytes(FIXTURE.read_bytes() + b"\n# valid but not approved on main\n")
    _git(repo, "add", "--", "backlog.yaml")
    _git(repo, "commit", "-q", "-m", "Untrusted artifact")
    artifact = load_artifact(backlog)
    envelope_value = json.loads(envelope.read_text(encoding="utf-8"))
    envelope_value.update(
        {
            "approved_commit": _git(repo, "rev-parse", "HEAD"),
            "backlog_sha256": artifact.sha256,
            "artifact_blob_sha1": artifact.blob_sha1,
        }
    )
    envelope.write_text(json.dumps(envelope_value), encoding="utf-8")
    monkeypatch.setenv("PLANNING_PUBLICATION_POLICY", str(policy))
    monkeypatch.setenv("OPENPROJECT_API_TOKEN", "must-not-be-used")

    assert (
        main(
            [
                "publish",
                str(backlog),
                "--apply",
                "--publisher-config",
                str(PUBLISHER_CONFIG),
                "--publication-envelope",
                str(envelope),
            ]
        )
        == 2
    )
    output = capsys.readouterr().out
    assert "not reachable from the trusted Git ref" in output
    assert "must-not-be-used" not in output


def test_apply_cli_policy_pins_openproject_target(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    backlog, envelope, policy = _apply_files(tmp_path)
    policy_value = json.loads(policy.read_text(encoding="utf-8"))
    policy_value["openproject_target_sha256"] = "f" * 64
    policy.write_text(json.dumps(policy_value), encoding="utf-8")
    monkeypatch.setenv("PLANNING_PUBLICATION_POLICY", str(policy))
    monkeypatch.setenv("OPENPROJECT_API_TOKEN", "must-not-be-used")

    assert (
        main(
            [
                "publish",
                str(backlog),
                "--apply",
                "--publisher-config",
                str(PUBLISHER_CONFIG),
                "--publication-envelope",
                str(envelope),
            ]
        )
        == 2
    )
    output = capsys.readouterr().out
    assert "does not match publication policy" in output
    assert "must-not-be-used" not in output


def test_apply_cli_policy_rejects_origin_path_and_symlink_drift(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    token = "must-not-be-used"
    monkeypatch.setenv("OPENPROJECT_API_TOKEN", token)

    def forbidden(*args, **kwargs):
        raise AssertionError("credentialed publication dependency was reached")

    monkeypatch.setattr("planning_platform.cli.OpenProjectPublicationAdapter", forbidden)
    monkeypatch.setattr("planning_platform.cli.PostgresPublicationJournal", forbidden)

    origin_backlog, origin_envelope, origin_policy = _apply_files(tmp_path / "origin")
    origin_value = json.loads(origin_policy.read_text(encoding="utf-8"))
    origin_value["expected_origin_url"] = "https://SECRET_ORIGIN.example.invalid/repo.git"
    origin_policy.write_text(json.dumps(origin_value), encoding="utf-8")

    path_backlog, path_envelope, path_policy = _apply_files(tmp_path / "path")
    outside_backlog = path_backlog.parent.parent / "outside-backlog.yaml"
    outside_backlog.write_bytes(path_backlog.read_bytes())

    symlink_backlog, symlink_envelope, symlink_policy = _apply_files(tmp_path / "symlink")
    symlink_target = symlink_backlog.with_name("backlog-target.yaml")
    symlink_backlog.rename(symlink_target)
    symlink_backlog.symlink_to(symlink_target.name)

    cases = (
        (origin_backlog, origin_envelope, origin_policy, "origin identity changed"),
        (outside_backlog, path_envelope, path_policy, "does not match"),
        (symlink_backlog, symlink_envelope, symlink_policy, "cannot contain symlinks"),
    )
    for backlog, envelope, policy, expected in cases:
        monkeypatch.setenv("PLANNING_PUBLICATION_POLICY", str(policy))
        assert (
            main(
                [
                    "publish",
                    str(backlog),
                    "--apply",
                    "--publisher-config",
                    str(PUBLISHER_CONFIG),
                    "--publication-envelope",
                    str(envelope),
                ]
            )
            == 2
        )
        output = capsys.readouterr().out
        assert expected in output
        assert "SECRET_ORIGIN" not in output
        assert token not in output


def test_apply_cli_rejects_ref_injection_and_suppresses_git_stderr(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    token = "must-not-be-used"
    monkeypatch.setenv("OPENPROJECT_API_TOKEN", token)

    for label, trusted_ref, expected in (
        ("injection", "refs/heads/main^{commit}", "trusted_ref is invalid"),
        ("missing", "refs/heads/SECRET_GIT_STDERR", "Git verification failed"),
    ):
        backlog, envelope, policy = _apply_files(tmp_path / label)
        policy_value = json.loads(policy.read_text(encoding="utf-8"))
        policy_value["trusted_ref"] = trusted_ref
        policy.write_text(json.dumps(policy_value), encoding="utf-8")
        monkeypatch.setenv("PLANNING_PUBLICATION_POLICY", str(policy))
        assert (
            main(
                [
                    "publish",
                    str(backlog),
                    "--apply",
                    "--publisher-config",
                    str(PUBLISHER_CONFIG),
                    "--publication-envelope",
                    str(envelope),
                ]
            )
            == 2
        )
        output = capsys.readouterr().out
        assert expected in output
        assert "SECRET_GIT_STDERR" not in output
        assert token not in output
