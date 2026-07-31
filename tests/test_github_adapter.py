from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from planning_platform.github_adapter import (
    GitHubAdapter,
    GitHubAdapterError,
    GitHubAppInstallationToken,
    StaticInstallationToken,
)
from planning_platform.github_models import ImmutableArtifactBinding, PlanningBranch


@pytest.mark.asyncio
async def test_immutable_artifact_verifies_commit_tree_blob_and_content_bytes() -> None:
    content = b"schema_version: 1.0.0\n"
    blob_sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
    commit = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token"
        if request.url.path.endswith(f"/commits/{commit}"):
            return httpx.Response(200, json={"sha": commit, "commit": {"tree": {"sha": "b" * 40}}})
        if "/git/trees/" in request.url.path:
            return httpx.Response(
                200,
                json={"tree": [{"path": "planning/backlog.yaml", "type": "blob", "sha": blob_sha}]},
            )
        if "/git/blobs/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "sha": blob_sha,
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode(),
                },
            )
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GitHubAdapter(
            client, StaticInstallationToken("token"), api_url="https://github.test"
        )
        actual = await adapter.read_immutable_artifact(
            ImmutableArtifactBinding(
                repository="RausserHQ/planning-platform",
                commit_sha=commit,
                path="planning/backlog.yaml",
                blob_sha=blob_sha,
                content_sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    assert actual == content


@pytest.mark.asyncio
async def test_planning_branch_refuses_unexpected_existing_commit() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"object": {"sha": "b" * 40}})
        )
    ) as client:
        adapter = GitHubAdapter(
            client, StaticInstallationToken("token"), api_url="https://github.test"
        )
        with pytest.raises(GitHubAdapterError, match="unexpected"):
            await adapter.ensure_planning_branch(
                PlanningBranch(
                    repository="RausserHQ/planning-platform",
                    name="planning/plan-1176",
                    commit_sha="a" * 40,
                )
            )


@pytest.mark.asyncio
async def test_planning_commit_creates_and_verifies_content_addressed_artifacts() -> None:
    content = b"schema_version: 1.0.0\n"
    blob_sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "GET" and path.endswith("/git/ref/heads/planning/demo"):
            return httpx.Response(404)
        if request.method == "GET" and path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "a" * 40}})
        if request.method == "GET" and path.endswith(f"/git/commits/{'a' * 40}"):
            return httpx.Response(200, json={"tree": {"sha": "b" * 40}})
        if request.method == "POST" and path.endswith("/git/blobs"):
            body = json.loads(request.content)
            assert base64.b64decode(body["content"]) == content
            return httpx.Response(201, json={"sha": blob_sha})
        if request.method == "POST" and path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "c" * 40})
        if request.method == "POST" and path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "d" * 40})
        if request.method == "POST" and path.endswith("/git/refs"):
            return httpx.Response(201, json={"object": {"sha": "d" * 40}})
        if request.method == "GET" and path.endswith(f"/commits/{'d' * 40}"):
            return httpx.Response(
                200,
                json={"sha": "d" * 40, "commit": {"tree": {"sha": "c" * 40}}},
            )
        if request.method == "GET" and path.endswith(f"/git/trees/{'c' * 40}"):
            return httpx.Response(
                200,
                json={
                    "truncated": False,
                    "tree": [
                        {
                            "path": "planning/demo/backlog.yaml",
                            "type": "blob",
                            "sha": blob_sha,
                        }
                    ],
                },
            )
        if request.method == "GET" and path.endswith(f"/git/blobs/{blob_sha}"):
            return httpx.Response(
                200,
                json={
                    "sha": blob_sha,
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode(),
                },
            )
        return httpx.Response(500, json={"path": path})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GitHubAdapter(client, StaticInstallationToken("token"))
        branch = await adapter.ensure_planning_commit(
            repository="owner/repo",
            branch_name="planning/demo",
            base="main",
            artifacts={"planning/demo/backlog.yaml": content},
            message="planning: demo",
        )
    assert branch.commit_sha == "d" * 40
    assert ("POST", "/repos/owner/repo/git/commits") in requests


@pytest.mark.asyncio
async def test_existing_planning_branch_is_accepted_only_after_byte_verification() -> None:
    content = b"approved"
    blob_sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/git/ref/heads/planning/retry"):
            return httpx.Response(200, json={"object": {"sha": "e" * 40}})
        if path.endswith(f"/commits/{'e' * 40}"):
            return httpx.Response(
                200,
                json={"sha": "e" * 40, "commit": {"tree": {"sha": "f" * 40}}},
            )
        if path.endswith(f"/git/trees/{'f' * 40}"):
            return httpx.Response(
                200,
                json={
                    "truncated": False,
                    "tree": [{"path": "planning/retry/SPEC.md", "type": "blob", "sha": blob_sha}],
                },
            )
        if path.endswith(f"/git/blobs/{blob_sha}"):
            return httpx.Response(
                200,
                json={
                    "sha": blob_sha,
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode(),
                },
            )
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GitHubAdapter(client, StaticInstallationToken("token"))
        branch = await adapter.ensure_planning_commit(
            repository="owner/repo",
            branch_name="planning/retry",
            base="main",
            artifacts={"planning/retry/SPEC.md": content},
            message="planning: retry",
        )
    assert branch.commit_sha == "e" * 40


@pytest.mark.asyncio
async def test_typed_planner_client_authenticates_and_parses_artifact_bundle() -> None:
    from planning_platform.lifecycle.planner_client import PlannerClient

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Planning-Internal-Token"] == "internal-token"
        assert request.url.path == "/v1/plans/openproject:42:planning:1/artifacts"
        return httpx.Response(200, json={"thread_id": "thread", "artifacts": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PlannerClient("https://planner.test", "internal-token", client).artifacts(
            "openproject:42:planning:1"
        )
    assert result.thread_id == "thread"


@pytest.mark.asyncio
async def test_github_app_provider_mints_short_lived_assertion_and_caches_installation_token() -> (
    None
):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/app/installations/1176/access_tokens"
        assertion = request.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(assertion, options={"verify_signature": False})
        assert claims["iss"] == "42"
        assert 0 < claims["exp"] - claims["iat"] <= 600
        return httpx.Response(
            201,
            json={
                "token": "installation-token",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GitHubAppInstallationToken(
            client,
            app_id=42,
            installation_id=1176,
            private_key_pem=pem,
            api_url="https://github.test",
        )
        assert await provider.token() == "installation-token"
        assert await provider.token() == "installation-token"
    assert calls == 1
