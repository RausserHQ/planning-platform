from __future__ import annotations

import pytest
from pydantic import ValidationError

from planning_platform.github_models import ImmutableArtifactBinding, PullRequestEvidence


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
        merged=True,
        merge_commit_sha="b" * 40,
        approvals=1,
        checks=(),
    )
    assert not evidence.required_checks_pass({"planner-validation"})
