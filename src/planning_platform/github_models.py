"""Typed GitHub boundary models. GitHub is evidence/source authority, never a ticket store."""

from __future__ import annotations

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
    name: str = Field(min_length=1)
    status: Literal["queued", "in_progress", "completed"]
    conclusion: str | None = None
    details_url: str | None = None


class PullRequestEvidence(GitHubModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    number: int = Field(ge=1)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    merged: bool
    merge_commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    approvals: int = Field(ge=0)
    checks: tuple[CheckEvidence, ...]

    def required_checks_pass(self, required: set[str]) -> bool:
        by_name = {check.name: check for check in self.checks}
        return all(
            name in by_name
            and by_name[name].status == "completed"
            and by_name[name].conclusion == "success"
            for name in required
        )
