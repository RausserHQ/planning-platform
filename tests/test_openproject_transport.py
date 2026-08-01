from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

from planning_platform import openproject_transport
from windmill.f.planning import verify_webhook


def _install_mock_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: httpx.MockTransport,
) -> None:
    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=handler, **kwargs)

    monkeypatch.setattr(openproject_transport.httpx, "Client", client)


def test_client_uses_internal_route_with_canonical_origin_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"id": 8})

    _install_mock_client(monkeypatch, httpx.MockTransport(handler))
    with openproject_transport.openproject_client(
        base_url="http://openproject.planning-platform.svc.cluster.local:8080",
        canonical_origin="https://openproject.apps.home.rausser.space",
        token="token-not-to-be-logged",
    ) as client:
        response = client.get("/api/v3/users/me")

    assert response.status_code == 200
    assert len(observed) == 1
    request = observed[0]
    assert str(request.url) == (
        "http://openproject.planning-platform.svc.cluster.local:8080/api/v3/users/me"
    )
    assert request.headers["host"] == "openproject.apps.home.rausser.space"
    assert request.headers["x-forwarded-proto"] == "https"
    assert request.headers["accept"] == "application/hal+json"
    credential = base64.b64encode(b"apikey:token-not-to-be-logged").decode("ascii")
    assert request.headers["authorization"] == f"Basic {credential}"


@pytest.mark.parametrize(
    "canonical_origin",
    (
        "",
        "openproject.example.test",
        "http://openproject.example.test",
        "ftp://openproject.example.test",
        "https://@openproject.example.test",
        "https://user@openproject.example.test",
        "https://openproject.example.test/api/v3",
        "https://openproject.example.test/.",
        "https://openproject.example.test ",
        "https://openproject.example.test?query=1",
        "https://openproject.example.test#fragment",
        "https://openproject.example.test:invalid",
        "https://openproject.example.test:0",
        "https://openproject.example.test:65536",
    ),
)
def test_client_rejects_values_that_are_not_canonical_origins(
    canonical_origin: str,
) -> None:
    with pytest.raises(ValueError, match="canonical origin"):
        openproject_transport.openproject_client(
            base_url="http://openproject.internal:8080",
            canonical_origin=canonical_origin,
            token="token-not-to-be-logged",
        )


def test_discovered_adapter_uses_one_client_for_discovery_and_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_client = object()
    config = object()
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        openproject_transport,
        "openproject_client",
        lambda **kwargs: observed.setdefault("client", shared_client),
    )

    def discover(**kwargs: object) -> object:
        observed["discovery_client"] = kwargs["client"]
        observed["discovery_token"] = kwargs["token"]
        observed["canonical_origin"] = kwargs["canonical_origin"]
        return config

    def adapter(
        supplied_config: object,
        supplied_token: str,
        *,
        client: object,
    ) -> object:
        observed["adapter_config"] = supplied_config
        observed["adapter_token"] = supplied_token
        observed["adapter_client"] = client
        return object()

    monkeypatch.setattr(openproject_transport, "discover_openproject_config", discover)
    monkeypatch.setattr(openproject_transport, "OpenProjectPublicationAdapter", adapter)

    result = openproject_transport.discover_openproject_adapter(
        base_url="http://openproject.internal:8080",
        canonical_origin="https://openproject.example.test",
        project_identifier="planning-platform",
        token="token-not-to-be-logged",
    )

    assert result is not None
    assert observed["discovery_client"] is shared_client
    assert observed["adapter_client"] is shared_client
    assert observed["adapter_config"] is config
    assert observed["canonical_origin"] == "https://openproject.example.test"
    assert observed["discovery_token"] == "token-not-to-be-logged"
    assert observed["adapter_token"] == "token-not-to-be-logged"


def test_discovered_adapter_closes_client_when_discovery_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        closed = False

        def close(self) -> None:
            self.closed = True

    client = Client()
    monkeypatch.setattr(openproject_transport, "openproject_client", lambda **kwargs: client)

    def fail(**kwargs: object) -> object:
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(openproject_transport, "discover_openproject_config", fail)

    with pytest.raises(RuntimeError, match="discovery failed"):
        openproject_transport.discover_openproject_adapter(
            base_url="http://openproject.internal:8080",
            canonical_origin="https://openproject.example.test",
            project_identifier="planning-platform",
            token="token-not-to-be-logged",
        )
    assert client.closed is True


def test_webhook_service_identity_uses_the_shared_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/users/me"
        return httpx.Response(200, json={"id": 8})

    monkeypatch.setenv("OPENPROJECT_BASE_URL", "http://openproject.internal:8080")
    monkeypatch.setenv(
        "OPENPROJECT_CANONICAL_ORIGIN",
        "https://openproject.example.test",
    )
    monkeypatch.setenv("OPENPROJECT_API_TOKEN", "token-not-to-be-logged")
    _install_mock_client(monkeypatch, httpx.MockTransport(handler))

    assert verify_webhook._openproject_service_actor_id() == "8"


def test_webhook_service_identity_error_exposes_only_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_body = "private-response-body"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=response_body)

    monkeypatch.setenv("OPENPROJECT_BASE_URL", "http://openproject.internal:8080")
    monkeypatch.setenv(
        "OPENPROJECT_CANONICAL_ORIGIN",
        "https://openproject.example.test",
    )
    monkeypatch.setenv("OPENPROJECT_API_TOKEN", "token-not-to-be-logged")
    _install_mock_client(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="returned 400") as raised:
        verify_webhook._openproject_service_actor_id()
    assert "token-not-to-be-logged" not in str(raised.value)
    assert response_body not in str(raised.value)
