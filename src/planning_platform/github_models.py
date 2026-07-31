"""Typed GitHub boundary models. GitHub is evidence/source authority, never a ticket store."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GitHubModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ImmutableArtifactBinding(GitHubModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    path: str = Field(min_length=1, max_length=512)
    blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlanningBranch(GitHubModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    name: str = Field(pattern=r"^planning/[a-z0-9][a-z0-9._/-]{2,120}$")
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class PlanningPullRequest(GitHubModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    number: int = Field(ge=1)
    url: str = Field(pattern=r"^https://")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    state: Literal["open", "closed"]
    merged: bool
    merge_commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class CheckEvidence(GitHubModel):
    id: int = Field(ge=1)
    name: str = Field(min_length=1)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    app_id: int = Field(ge=1)
    app_slug: str = Field(min_length=1)
    status: Literal["queued", "in_progress", "completed"]
    conclusion: str | None = None
    details_url: str | None = None


class ReviewEvidence(GitHubModel):
    id: int = Field(ge=1)
    actor_id: int = Field(ge=1)
    actor_login: str = Field(min_length=1)
    state: Literal["APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"]
    submitted_at: datetime
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class PullRequestEvidence(GitHubModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    number: int = Field(ge=1)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    state: Literal["open", "closed"]
    merged: bool
    merge_commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    reviews: tuple[ReviewEvidence, ...]
    checks: tuple[CheckEvidence, ...]

    @property
    def approvals(self) -> int:
        return sum(
            review.state == "APPROVED" and review.commit_sha == self.head_sha
            for review in self.reviews
        )

    def required_checks_pass(
        self,
        required: set[str],
        *,
        trusted_app_slug: str = "github-actions",
    ) -> bool:
        by_name: dict[str, list[CheckEvidence]] = {}
        for check in self.checks:
            if check.app_slug == trusted_app_slug and check.head_sha == self.head_sha:
                by_name.setdefault(check.name, []).append(check)
        return all(
            name in by_name
            and bool(by_name[name])
            and all(
                check.status == "completed" and check.conclusion == "success"
                for check in by_name[name]
            )
            for name in required
        )
