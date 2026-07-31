from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from planning_platform.github_models import (
    CheckEvidence,
    ImmutableArtifactBinding,
    PullRequestEvidence,
    ReviewEvidence,
)

ROOT = Path(__file__).resolve().parents[1]


def test_immutable_artifact_binding_requires_full_commit_blob_and_content_identities() -> None:
    binding = ImmutableArtifactBinding(
        repository="RausserHQ/planning-platform",
        commit_sha="a" * 40,
        path="planning/backlog.yaml",
        blob_sha="b" * 40,
        content_sha256="c" * 64,
    )
    assert binding.commit_sha == "a" * 40
    with pytest.raises(ValidationError):
        ImmutableArtifactBinding(
            repository="not a repo",
            commit_sha="short",
            path="",
            blob_sha="bad",
            content_sha256="bad",
        )


def test_required_check_evidence_is_explicit_and_fail_closed() -> None:
    evidence = PullRequestEvidence(
        repository="RausserHQ/planning-platform",
        number=12,
        head_sha="a" * 40,
        state="closed",
        merged=True,
        merge_commit_sha="b" * 40,
        reviews=(),
        checks=(),
    )
    assert not evidence.required_checks_pass({"planner-validation"})


def test_review_and_check_evidence_bind_current_head_actor_and_trusted_app() -> None:
    head_sha = "a" * 40
    stale_review = ReviewEvidence.model_validate(
        {
            "id": 17,
            "actor_id": 7,
            "actor_login": "reviewer",
            "state": "APPROVED",
            "submitted_at": "2026-07-30T19:00:00Z",
            "commit_sha": "b" * 40,
        }
    )
    untrusted_check = CheckEvidence(
        id=18,
        name="planning-backlog-validation",
        head_sha=head_sha,
        app_id=99,
        app_slug="untrusted-app",
        status="completed",
        conclusion="success",
    )
    evidence = PullRequestEvidence(
        repository="RausserHQ/planning-platform",
        number=12,
        head_sha=head_sha,
        state="closed",
        merged=True,
        merge_commit_sha="c" * 40,
        reviews=(stale_review,),
        checks=(untrusted_check,),
    )

    assert evidence.approvals == 0
    assert not evidence.required_checks_pass({"planning-backlog-validation"})


def test_ci_exposes_a_stable_fail_closed_planning_backlog_check() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["planning-backlog-validation"]
    assert job["name"] == "planning-backlog-validation"
    assert job["if"] == "github.event_name == 'pull_request'"
    checkout = job["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0
    run = job["steps"][-1]["run"]
    assert "git diff --name-only --diff-filter=ACMR" in run
    assert "'planning/**/backlog.yaml'" in run
    assert 'uv run --frozen planning validate "${GITHUB_WORKSPACE}/${backlog}"' in run
