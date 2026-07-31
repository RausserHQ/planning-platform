"""Fail-closed GitHub App REST adapter with immutable artifact and PR evidence checks."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol, cast

import httpx
import jwt

from .github_models import (
    CheckEvidence,
    ImmutableArtifactBinding,
    PlanningBranch,
    PlanningPullRequest,
    PullRequestEvidence,
    ReviewEvidence,
)
from .planner.models import RepositoryFile, RepositorySnapshot, repository_snapshot_digest


class GitHubAdapterError(RuntimeError):
    pass


class GitHubInstallationTokenProvider(Protocol):
    """Inject short-lived installation tokens; private keys never enter this adapter."""

    async def token(self) -> str: ...


class StaticInstallationToken:
    """Only for controlled tests or a secret-injected Windmill runtime."""

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("GitHub installation token is required")
        self._value = value

    async def token(self) -> str:
        return self._value


class GitHubAppInstallationToken:
    """Mint and cache short-lived installation tokens from a GitHub App key."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        app_id: int,
        installation_id: int,
        private_key_pem: str,
        api_url: str = "https://api.github.com",
    ) -> None:
        if app_id <= 0 or installation_id <= 0 or "PRIVATE KEY" not in private_key_pem:
            raise ValueError("valid GitHub App identity and private key are required")
        self._client = client
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = private_key_pem
        self._api_url = api_url.rstrip("/")
        self._cached: tuple[str, datetime] | None = None
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        now = datetime.now(UTC)
        cached = self._cached
        if cached is not None and cached[1] > now + timedelta(minutes=1):
            return cached[0]
        async with self._lock:
            now = datetime.now(UTC)
            cached = self._cached
            if cached is not None and cached[1] > now + timedelta(minutes=1):
                return cached[0]
            issued_at = now - timedelta(seconds=30)
            assertion = jwt.encode(
                {
                    "iat": int(issued_at.timestamp()),
                    "exp": int((now + timedelta(minutes=9)).timestamp()),
                    "iss": str(self._app_id),
                },
                self._private_key,
                algorithm="RS256",
            )
            response = await self._client.post(
                (f"{self._api_url}/app/installations/{self._installation_id}/access_tokens"),
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {assertion}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            if response.status_code != 201:
                raise GitHubAdapterError(
                    f"GitHub installation-token request failed with status {response.status_code}"
                )
            try:
                body = response.json()
                value = body["token"]
                expires = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError) as error:
                raise GitHubAdapterError(
                    "GitHub installation-token response is malformed"
                ) from error
            if (
                not isinstance(value, str)
                or not value
                or expires.tzinfo is None
                or expires <= now + timedelta(minutes=1)
            ):
                raise GitHubAdapterError("GitHub installation token is empty or already expiring")
            self._cached = value, expires.astimezone(UTC)
            return value


class GitHubAdapter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        token_provider: GitHubInstallationTokenProvider,
        *,
        api_url: str = "https://api.github.com",
    ) -> None:
        self._client = client
        self._tokens = token_provider
        self._api_url = api_url.rstrip("/")

    async def read_immutable_artifact(self, binding: ImmutableArtifactBinding) -> bytes:
        """Verify commit/tree/blob/content identity before an artifact reaches publication."""
        commit = await self._request(
            "GET", f"/repos/{binding.repository}/commits/{binding.commit_sha}"
        )
        if commit.get("sha") != binding.commit_sha:
            raise GitHubAdapterError(
                "GitHub commit response does not match requested immutable SHA"
            )
        tree = commit.get("commit", {}).get("tree", {})
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str) or len(tree_sha) != 40:
            raise GitHubAdapterError("GitHub commit has no immutable tree SHA")
        tree_response = await self._request(
            "GET", f"/repos/{binding.repository}/git/trees/{tree_sha}?recursive=1"
        )
        if tree_response.get("truncated") is True:
            raise GitHubAdapterError(
                "GitHub tree response is truncated and cannot bind an artifact"
            )
        entries = tree_response.get("tree")
        if not isinstance(entries, list):
            raise GitHubAdapterError("GitHub tree response is malformed")
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("path") == binding.path
            and entry.get("type") == "blob"
        ]
        if len(matches) != 1 or matches[0].get("sha") != binding.blob_sha:
            raise GitHubAdapterError("artifact path does not resolve to the approved blob")
        blob = await self._request(
            "GET", f"/repos/{binding.repository}/git/blobs/{binding.blob_sha}"
        )
        if blob.get("sha") != binding.blob_sha or blob.get("encoding") != "base64":
            raise GitHubAdapterError("GitHub blob response is malformed or mismatched")
        encoded = blob.get("content")
        if not isinstance(encoded, str):
            raise GitHubAdapterError("GitHub blob has no content")
        try:
            content = base64.b64decode(encoded.replace("\n", ""), validate=True)
        except ValueError as error:
            raise GitHubAdapterError("GitHub blob content is not valid base64") from error
        git_blob_sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        if git_blob_sha != binding.blob_sha:
            raise GitHubAdapterError("GitHub blob bytes do not match blob SHA")
        if hashlib.sha256(content).hexdigest() != binding.content_sha256:
            raise GitHubAdapterError("GitHub blob bytes do not match artifact SHA-256")
        return content

    async def repository_snapshot(
        self,
        *,
        repository: str,
        commit_sha: str,
        paths: tuple[str, ...],
        max_total_bytes: int = 2_097_152,
    ) -> RepositorySnapshot:
        """Read an explicit, bounded set of UTF-8 files at one commit."""
        if not paths or len(paths) > 500 or max_total_bytes <= 0:
            raise ValueError("repository snapshot paths and size must be bounded")
        canonical_paths = tuple(self._artifact_path(path) for path in paths)
        if len(set(canonical_paths)) != len(canonical_paths):
            raise ValueError("repository snapshot paths must be unique")
        commit = await self._request("GET", f"/repos/{repository}/commits/{commit_sha}")
        if not isinstance(commit, dict) or commit.get("sha") != commit_sha:
            raise GitHubAdapterError("repository snapshot commit does not match requested SHA")
        tree_value = commit.get("commit")
        tree_value = tree_value.get("tree") if isinstance(tree_value, dict) else None
        tree_sha = tree_value.get("sha") if isinstance(tree_value, dict) else None
        if not isinstance(tree_sha, str) or len(tree_sha) != 40:
            raise GitHubAdapterError("repository snapshot commit has no immutable tree")
        tree = await self._request("GET", f"/repos/{repository}/git/trees/{tree_sha}?recursive=1")
        if not isinstance(tree, dict) or tree.get("truncated") is True:
            raise GitHubAdapterError("repository snapshot tree is malformed or truncated")
        entries = tree.get("tree")
        if not isinstance(entries, list):
            raise GitHubAdapterError("repository snapshot tree has no entries")
        by_path = {
            str(entry["path"]): entry
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("type") == "blob"
            and isinstance(entry.get("path"), str)
            and isinstance(entry.get("sha"), str)
        }
        files: list[RepositoryFile] = []
        total = 0
        for path in canonical_paths:
            entry = by_path.get(path)
            if entry is None:
                raise GitHubAdapterError(f"repository snapshot path {path!r} is absent")
            size = entry.get("size")
            if type(size) is int and (size < 0 or size > max_total_bytes - total):
                raise GitHubAdapterError("repository snapshot exceeds its byte bound")
            blob_sha = str(entry["sha"])
            blob = await self._request("GET", f"/repos/{repository}/git/blobs/{blob_sha}")
            if not isinstance(blob, dict) or blob.get("encoding") != "base64":
                raise GitHubAdapterError("repository snapshot blob is malformed")
            encoded = blob.get("content")
            if not isinstance(encoded, str):
                raise GitHubAdapterError("repository snapshot blob has no content")
            try:
                content_bytes = base64.b64decode(encoded.replace("\n", ""), validate=True)
                content = content_bytes.decode("utf-8")
            except (UnicodeDecodeError, ValueError) as error:
                raise GitHubAdapterError("repository snapshot files must be valid UTF-8") from error
            actual_blob = hashlib.sha1(
                f"blob {len(content_bytes)}\0".encode() + content_bytes
            ).hexdigest()
            if actual_blob != blob_sha:
                raise GitHubAdapterError("repository snapshot blob bytes do not match SHA")
            total += len(content_bytes)
            if total > max_total_bytes:
                raise GitHubAdapterError("repository snapshot exceeds its byte bound")
            files.append(
                RepositoryFile(
                    path=path,
                    sha256=hashlib.sha256(content_bytes).hexdigest(),
                    content=content,
                )
            )
        values = tuple(files)
        return RepositorySnapshot(
            name=repository,
            commit=commit_sha,
            snapshot_sha256=repository_snapshot_digest(repository, commit_sha, values),
            files=values,
        )

    async def repository_head(self, repository: str) -> tuple[str, str]:
        """Return the repository default branch and its immutable head SHA."""
        metadata = await self._request("GET", f"/repos/{repository}")
        branch = metadata.get("default_branch") if isinstance(metadata, dict) else None
        if not isinstance(branch, str) or not branch:
            raise GitHubAdapterError("GitHub repository has no default branch")
        reference = await self._request("GET", f"/repos/{repository}/git/ref/heads/{branch}")
        sha = self._ref_sha(reference)
        if not isinstance(sha, str) or len(sha) != 40:
            raise GitHubAdapterError("GitHub default branch has no immutable head")
        return branch, sha

    async def context_snapshot(
        self, repository: str, *, max_files: int = 120
    ) -> tuple[str, RepositorySnapshot]:
        """Select deterministic planning context from a default-branch tree."""
        if max_files < 1 or max_files > 500:
            raise ValueError("context snapshot file bound must be between 1 and 500")
        branch, commit_sha = await self.repository_head(repository)
        commit = await self._request("GET", f"/repos/{repository}/commits/{commit_sha}")
        tree = commit.get("commit", {}).get("tree", {}) if isinstance(commit, dict) else {}
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str) or len(tree_sha) != 40:
            raise GitHubAdapterError("repository context commit has no immutable tree")
        document = await self._request(
            "GET", f"/repos/{repository}/git/trees/{tree_sha}?recursive=1"
        )
        if not isinstance(document, dict) or document.get("truncated") is True:
            raise GitHubAdapterError("repository context tree is malformed or truncated")
        entries = document.get("tree")
        if not isinstance(entries, list):
            raise GitHubAdapterError("repository context tree has no entries")

        def score(path: str) -> tuple[int, str]:
            name = PurePosixPath(path).name.casefold()
            if name == "agents.md":
                return 0, path
            if "/" not in path and name.startswith("readme"):
                return 1, path
            if "/" not in path and name in {
                "pyproject.toml",
                "package.json",
                "cargo.toml",
                "go.mod",
                "makefile",
            }:
                return 2, path
            if path.startswith("docs/") and path.endswith((".md", ".yaml", ".yml")):
                return 3, path
            if path.startswith(".github/workflows/") and path.endswith((".yaml", ".yml")):
                return 4, path
            if path.endswith((".py", ".ts", ".tsx", ".go", ".rs", ".tf", ".yaml", ".yml")):
                return 5, path
            return 99, path

        candidates: list[str] = []
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or entry.get("type") != "blob"
                or not isinstance(entry.get("path"), str)
            ):
                continue
            path = str(entry["path"])
            size = entry.get("size")
            if type(size) is int and size > 262_144:
                continue
            if score(path)[0] < 99:
                candidates.append(path)
        selected = tuple(sorted(candidates, key=score)[:max_files])
        if not selected:
            raise GitHubAdapterError("repository has no bounded textual planning context")
        return branch, await self.repository_snapshot(
            repository=repository,
            commit_sha=commit_sha,
            paths=selected,
        )

    async def artifact_binding(
        self, *, repository: str, commit_sha: str, path: str
    ) -> ImmutableArtifactBinding:
        """Resolve and verify one path into a complete immutable binding."""
        canonical_path = self._artifact_path(path)
        commit = await self._request("GET", f"/repos/{repository}/commits/{commit_sha}")
        if not isinstance(commit, dict) or commit.get("sha") != commit_sha:
            raise GitHubAdapterError("artifact binding commit does not match requested SHA")
        tree = commit.get("commit", {}).get("tree", {})
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str) or len(tree_sha) != 40:
            raise GitHubAdapterError("artifact binding commit has no immutable tree")
        document = await self._request(
            "GET", f"/repos/{repository}/git/trees/{tree_sha}?recursive=1"
        )
        if not isinstance(document, dict) or document.get("truncated") is True:
            raise GitHubAdapterError("artifact binding tree is malformed or truncated")
        entries = document.get("tree")
        matches = (
            [
                entry
                for entry in entries
                if isinstance(entry, dict)
                and entry.get("path") == canonical_path
                and entry.get("type") == "blob"
                and isinstance(entry.get("sha"), str)
            ]
            if isinstance(entries, list)
            else []
        )
        if len(matches) != 1:
            raise GitHubAdapterError("artifact binding path is absent or duplicated")
        blob_sha = str(matches[0]["sha"])
        blob = await self._request("GET", f"/repos/{repository}/git/blobs/{blob_sha}")
        encoded = blob.get("content") if isinstance(blob, dict) else None
        if (
            not isinstance(blob, dict)
            or blob.get("sha") != blob_sha
            or blob.get("encoding") != "base64"
            or not isinstance(encoded, str)
        ):
            raise GitHubAdapterError("artifact binding blob is malformed")
        try:
            content = base64.b64decode(encoded.replace("\n", ""), validate=True)
        except ValueError as error:
            raise GitHubAdapterError("artifact binding blob is not valid base64") from error
        binding = ImmutableArtifactBinding(
            repository=repository,
            commit_sha=commit_sha,
            path=canonical_path,
            blob_sha=blob_sha,
            content_sha256=hashlib.sha256(content).hexdigest(),
        )
        await self.read_immutable_artifact(binding)
        return binding

    async def ensure_planning_commit(
        self,
        *,
        repository: str,
        branch_name: str,
        base: str,
        artifacts: Mapping[str, bytes],
        message: str,
    ) -> PlanningBranch:
        """Create one immutable artifact commit, or verify a retry's branch."""
        if not artifacts or not message.strip():
            raise ValueError("planning commit requires artifacts and a commit message")
        normalized = {
            self._artifact_path(path): bytes(content) for path, content in artifacts.items()
        }
        if len(normalized) != len(artifacts):
            raise ValueError("planning artifact paths are duplicated after normalization")
        branch_probe = PlanningBranch(
            repository=repository,
            name=branch_name,
            commit_sha="0" * 40,
        )
        encoded_name = branch_probe.name.replace("/", "%2F")
        existing = await self._request_or_none(
            "GET", f"/repos/{repository}/git/ref/heads/{encoded_name}"
        )
        if existing is not None:
            sha = self._ref_sha(existing)
            if not isinstance(sha, str):
                raise GitHubAdapterError("existing planning branch has no commit SHA")
            branch = PlanningBranch(repository=repository, name=branch_name, commit_sha=sha)
            await self._verify_commit_artifacts(branch, normalized)
            return branch

        base_ref = await self._request("GET", f"/repos/{repository}/git/ref/heads/{base}")
        base_sha = self._ref_sha(base_ref)
        if not isinstance(base_sha, str) or len(base_sha) != 40:
            raise GitHubAdapterError("GitHub base branch has no immutable commit")
        base_commit = await self._request("GET", f"/repos/{repository}/git/commits/{base_sha}")
        base_tree = (
            base_commit.get("tree", {}).get("sha")
            if isinstance(base_commit, dict) and isinstance(base_commit.get("tree"), dict)
            else None
        )
        if not isinstance(base_tree, str) or len(base_tree) != 40:
            raise GitHubAdapterError("GitHub base commit has no immutable tree")

        entries: list[dict[str, str]] = []
        for path, content in sorted(normalized.items()):
            blob = await self._request(
                "POST",
                f"/repos/{repository}/git/blobs",
                {
                    "content": base64.b64encode(content).decode("ascii"),
                    "encoding": "base64",
                },
            )
            blob_sha = blob.get("sha") if isinstance(blob, dict) else None
            expected_sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
            if blob_sha != expected_sha:
                raise GitHubAdapterError("GitHub created an unexpected artifact blob")
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": expected_sha})
        tree = await self._request(
            "POST",
            f"/repos/{repository}/git/trees",
            {"base_tree": base_tree, "tree": entries},
        )
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str) or len(tree_sha) != 40:
            raise GitHubAdapterError("GitHub created tree has no immutable SHA")
        commit = await self._request(
            "POST",
            f"/repos/{repository}/git/commits",
            {"message": message, "tree": tree_sha, "parents": [base_sha]},
        )
        commit_sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(commit_sha, str) or len(commit_sha) != 40:
            raise GitHubAdapterError("GitHub created commit has no immutable SHA")
        branch = PlanningBranch(
            repository=repository,
            name=branch_name,
            commit_sha=commit_sha,
        )
        await self.ensure_planning_branch(branch)
        await self._verify_commit_artifacts(branch, normalized)
        return branch

    async def ensure_planning_branch(self, branch: PlanningBranch) -> PlanningBranch:
        encoded_name = branch.name.replace("/", "%2F")
        result = await self._request_or_none(
            "GET", f"/repos/{branch.repository}/git/ref/heads/{encoded_name}"
        )
        if result is None:
            created = await self._request(
                "POST",
                f"/repos/{branch.repository}/git/refs",
                {"ref": f"refs/heads/{branch.name}", "sha": branch.commit_sha},
            )
            actual = self._ref_sha(created)
        else:
            actual = self._ref_sha(result)
        if actual != branch.commit_sha:
            raise GitHubAdapterError("planning branch exists at an unexpected immutable commit")
        return branch

    async def _verify_commit_artifacts(
        self, branch: PlanningBranch, artifacts: Mapping[str, bytes]
    ) -> None:
        commit = await self._request(
            "GET", f"/repos/{branch.repository}/commits/{branch.commit_sha}"
        )
        tree = commit.get("commit", {}).get("tree", {}) if isinstance(commit, dict) else {}
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str) or len(tree_sha) != 40:
            raise GitHubAdapterError("planning commit has no immutable tree")
        document = await self._request(
            "GET", f"/repos/{branch.repository}/git/trees/{tree_sha}?recursive=1"
        )
        if not isinstance(document, dict) or document.get("truncated") is True:
            raise GitHubAdapterError("planning commit tree is malformed or truncated")
        entries = document.get("tree")
        if not isinstance(entries, list):
            raise GitHubAdapterError("planning commit tree has no entries")
        by_path = {
            str(entry["path"]): str(entry["sha"])
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("type") == "blob"
            and isinstance(entry.get("path"), str)
            and isinstance(entry.get("sha"), str)
        }
        for path, content in artifacts.items():
            blob_sha = by_path.get(path)
            expected_blob = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
            if blob_sha != expected_blob:
                raise GitHubAdapterError(
                    f"planning artifact {path!r} is absent or has unexpected bytes"
                )
            await self.read_immutable_artifact(
                ImmutableArtifactBinding(
                    repository=branch.repository,
                    commit_sha=branch.commit_sha,
                    path=path,
                    blob_sha=expected_blob,
                    content_sha256=hashlib.sha256(content).hexdigest(),
                )
            )

    @staticmethod
    def _artifact_path(value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or len(value) > 512
            or path.is_absolute()
            or str(path) != value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("planning artifact path must be canonical and repository-relative")
        return value

    async def ensure_planning_pull_request(
        self,
        branch: PlanningBranch,
        *,
        base: str,
        title: str,
        body: str,
    ) -> PlanningPullRequest:
        owner, _repository = branch.repository.split("/", 1)
        result = await self._request(
            "GET",
            f"/repos/{branch.repository}/pulls?state=open&head={owner}:{branch.name}",
        )
        if not isinstance(result, list):
            raise GitHubAdapterError("GitHub pull request search is malformed")
        if len(result) > 1:
            raise GitHubAdapterError("more than one open planning pull request exists")
        if result:
            pull = result[0]
        else:
            pull = await self._request(
                "POST",
                f"/repos/{branch.repository}/pulls",
                {"title": title, "head": branch.name, "base": base, "body": body},
            )
        model = self._pull_request(branch.repository, pull)
        if model.head_sha != branch.commit_sha:
            raise GitHubAdapterError("planning PR head does not match immutable planning branch")
        return model

    async def pull_request_evidence(
        self,
        repository: str,
        number: int,
        *,
        expected_head_sha: str | None = None,
    ) -> PullRequestEvidence:
        pull_value = await self._request("GET", f"/repos/{repository}/pulls/{number}")
        pull = self._pull_request(repository, pull_value)
        if expected_head_sha is not None and pull.head_sha != expected_head_sha:
            raise GitHubAdapterError(
                "pull request evidence is stale for the expected commit"
            )
        reviews: list[Any] = []
        for page in range(1, 11):
            review_page = await self._request(
                "GET",
                f"/repos/{repository}/pulls/{number}/reviews"
                f"?per_page=100&page={page}",
            )
            if not isinstance(review_page, list):
                raise GitHubAdapterError("GitHub review evidence is malformed")
            reviews.extend(review_page)
            if len(review_page) < 100:
                break
        else:
            raise GitHubAdapterError(
                "GitHub review evidence exceeds the bounded pagination limit"
            )
        check_values: list[Any] = []
        check_total: int | None = None
        for page in range(1, 11):
            checks = await self._request(
                "GET",
                f"/repos/{repository}/commits/{pull.head_sha}/check-runs"
                f"?filter=latest&per_page=100&page={page}",
            )
            values = checks.get("check_runs") if isinstance(checks, dict) else None
            total = checks.get("total_count") if isinstance(checks, dict) else None
            if not isinstance(values, list) or type(total) is not int or total < 0:
                raise GitHubAdapterError("GitHub check evidence is malformed")
            if check_total is None:
                check_total = total
            elif check_total != total:
                raise GitHubAdapterError("GitHub check evidence changed during pagination")
            check_values.extend(values)
            if len(values) < 100:
                break
        else:
            raise GitHubAdapterError(
                "GitHub check evidence exceeds the bounded pagination limit"
            )
        if check_total != len(check_values):
            raise GitHubAdapterError("GitHub check evidence is incomplete")

        latest_reviews: dict[
            int,
            tuple[tuple[datetime, int, int], ReviewEvidence],
        ] = {}
        for index, review in enumerate(reviews):
            if not isinstance(review, dict) or not isinstance(review.get("user"), dict):
                raise GitHubAdapterError("GitHub review evidence is malformed")
            user = review["user"]
            user_id = user.get("id")
            if user.get("type") != "User":
                continue
            if review.get("state") in {"PENDING", "COMMENTED"}:
                continue
            submitted_at_value = review.get("submitted_at")
            review_id = review.get("id")
            actor_login = user.get("login")
            review_state = review.get("state")
            commit_sha = review.get("commit_id")
            if (
                type(user_id) is not int
                or type(review_id) is not int
                or not isinstance(actor_login, str)
                or not isinstance(submitted_at_value, str)
                or review_state
                not in {
                    "APPROVED",
                    "CHANGES_REQUESTED",
                    "DISMISSED",
                }
                or not isinstance(commit_sha, str)
            ):
                raise GitHubAdapterError("GitHub review evidence is malformed")
            try:
                submitted_at = datetime.fromisoformat(
                    submitted_at_value.replace("Z", "+00:00")
                )
                evidence_review = ReviewEvidence(
                    id=review_id,
                    actor_id=user_id,
                    actor_login=actor_login,
                    state=cast(
                        Literal[
                            "APPROVED",
                            "CHANGES_REQUESTED",
                            "DISMISSED",
                        ],
                        review_state,
                    ),
                    submitted_at=submitted_at,
                    commit_sha=commit_sha,
                )
            except (TypeError, ValueError) as error:
                raise GitHubAdapterError("GitHub review evidence is malformed") from error
            if submitted_at.tzinfo is None:
                raise GitHubAdapterError("GitHub review evidence timestamp has no timezone")
            order = (submitted_at, evidence_review.id, index)
            current = latest_reviews.get(user_id)
            if current is None or order > current[0]:
                latest_reviews[user_id] = (order, evidence_review)
        evidence_checks: list[CheckEvidence] = []
        for entry in check_values:
            if not isinstance(entry, dict) or not isinstance(entry.get("app"), dict):
                raise GitHubAdapterError("GitHub check evidence is malformed")
            app = entry["app"]
            check_id = entry.get("id")
            check_name = entry.get("name")
            check_head = entry.get("head_sha")
            app_id = app.get("id")
            app_slug = app.get("slug")
            check_status = entry.get("status")
            if (
                type(check_id) is not int
                or not isinstance(check_name, str)
                or not isinstance(check_head, str)
                or type(app_id) is not int
                or not isinstance(app_slug, str)
                or check_status not in {"queued", "in_progress", "completed"}
            ):
                raise GitHubAdapterError("GitHub check evidence is malformed")
            try:
                evidence_checks.append(
                    CheckEvidence(
                        id=check_id,
                        name=check_name,
                        head_sha=check_head,
                        app_id=app_id,
                        app_slug=app_slug,
                        status=cast(
                            Literal["queued", "in_progress", "completed"],
                            check_status,
                        ),
                        conclusion=entry.get("conclusion"),
                        details_url=entry.get("details_url"),
                    )
                )
            except ValueError as error:
                raise GitHubAdapterError("GitHub check evidence is malformed") from error
        return PullRequestEvidence(
            repository=repository,
            number=number,
            head_sha=pull.head_sha,
            state=pull.state,
            merged=pull.merged,
            merge_commit_sha=pull.merge_commit_sha,
            reviews=tuple(value[1] for value in latest_reviews.values()),
            checks=tuple(evidence_checks),
        )

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        result = await self._request_or_none(method, path, body)
        if result is None:
            raise GitHubAdapterError("GitHub resource was not found")
        return result

    async def _request_or_none(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any | None:
        token = await self._tokens.token()
        response = await self._client.request(
            method,
            f"{self._api_url}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=body,
        )
        if response.status_code == 404:
            return None
        if response.status_code < 200 or response.status_code >= 300:
            raise GitHubAdapterError(
                f"GitHub API request failed with status {response.status_code}"
            )
        try:
            return response.json()
        except ValueError as error:
            raise GitHubAdapterError("GitHub API response is not JSON") from error

    @staticmethod
    def _ref_sha(value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        object_value = value.get("object")
        return object_value.get("sha") if isinstance(object_value, dict) else None

    @staticmethod
    def _pull_request(repository: str, value: Any) -> PlanningPullRequest:
        if not isinstance(value, dict):
            raise GitHubAdapterError("GitHub pull request response is malformed")
        head = value.get("head")
        if not isinstance(head, dict):
            raise GitHubAdapterError("GitHub pull request has no head")
        try:
            return PlanningPullRequest(
                repository=repository,
                number=value["number"],
                url=value["html_url"],
                head_sha=head["sha"],
                state=value["state"],
                merged=bool(value.get("merged", value.get("merged_at") is not None)),
                merge_commit_sha=value.get("merge_commit_sha"),
            )
        except (KeyError, ValueError) as error:
            raise GitHubAdapterError("GitHub pull request fields are invalid") from error
