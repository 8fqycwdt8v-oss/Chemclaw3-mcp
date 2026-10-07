"""The credential is checked, the probes stay open, and a misconfigured server serves nothing.

The middleware is only a control if a test proves the refusal.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from mcp_server_kit.auth import OPEN_PATHS, BearerAuthMiddleware, BodySizeLimit, _is_open

TOKEN_ENV = "TEST_SERVER_TOKEN"


async def _ok() -> dict[str, str]:
    """The body an open probe route answers with; the status is what these tests are about."""
    return {"status": "ok"}


def _app(*, token_env: str | None, max_bytes: int = 0) -> FastAPI:
    """A minimal app carrying the same middleware stack `connector_app` installs."""
    app = FastAPI()
    app.add_middleware(BearerAuthMiddleware, server="test", token_env=token_env)
    if max_bytes:
        app.add_middleware(BodySizeLimit, max_bytes=max_bytes)

    # One route per open path, both spellings, so a test that expects 200 is testing the middleware
    # rather than the route table — a missing route answers 404, which is *also* not 401 and is how
    # the trailing-slash test below once passed while every probe in the fleet got 404.
    for path in sorted(OPEN_PATHS):
        app.add_api_route(path, _ok, methods=["GET"])
        app.add_api_route(f"{path}/", _ok, methods=["GET"])

    @app.post("/mcp")
    async def mcp() -> dict[str, str]:
        return {"served": "yes"}

    return app


async def _call(app: FastAPI, method: str, path: str, **kwargs: object) -> httpx.Response:
    """Drive the app in-process over ASGI — no socket, so no egress question arises."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("path", sorted(OPEN_PATHS))
async def test_every_probe_route_is_open(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """A kubelet probe and a Prometheus scrape have no identity, and carry nothing to protect.

    Parametrized over `OPEN_PATHS` so a new open route is covered the day it appears; a liveness
    probe refused with 401 kills the container.
    """
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    response = await _call(_app(token_env=TOKEN_ENV), "GET", path)
    assert response.status_code == 200


async def test_the_mcp_surface_refuses_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal that did not exist in Chemclaw3 until an unauthenticated handshake proved it."""
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    response = await _call(_app(token_env=TOKEN_ENV), "POST", "/mcp")
    assert response.status_code == 401


async def test_the_right_token_is_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """And the credential actually lets a caller through, or the server would be unusable."""
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    response = await _call(
        _app(token_env=TOKEN_ENV),
        "POST",
        "/mcp",
        headers={"authorization": "Bearer s3cret"},
    )
    assert response.status_code == 200


async def test_a_wrong_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comparison is constant-time, but the outcome is what a test can see."""
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    response = await _call(
        _app(token_env=TOKEN_ENV), "POST", "/mcp", headers={"authorization": "Bearer wrong"}
    )
    assert response.status_code == 401


async def test_a_missing_env_var_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared token_env whose variable is unset serves nothing — never everything."""
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    response = await _call(
        _app(token_env=TOKEN_ENV), "POST", "/mcp", headers={"authorization": "Bearer anything"}
    )
    assert response.status_code == 401


async def test_a_non_ascii_header_is_refused_not_crashed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comparing as `str` would raise TypeError here — a 500 any remote party could trigger.

    Sent as raw bytes: Starlette decodes headers as latin-1, so a byte above 0x7F reaches the
    comparison as a non-ASCII `str`. An httpx `str` header would be rejected client-side.
    """
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    response = await _call(
        _app(token_env=TOKEN_ENV),
        "POST",
        "/mcp",
        headers={"authorization": "Bearer s3crét".encode("latin-1")},
    )
    assert response.status_code == 401


async def test_mode_none_passes_through() -> None:
    """A loopback dev server declaring no credential is served without one."""
    response = await _call(_app(token_env=None), "POST", "/mcp")
    assert response.status_code == 200


async def test_an_oversized_body_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """One MCP call carries chemistry-sized arguments, never a file."""
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    app = _app(token_env=TOKEN_ENV, max_bytes=64)
    response = await _call(
        app,
        "POST",
        "/mcp",
        headers={"authorization": "Bearer s3cret"},
        content=b"x" * 4096,
    )
    assert response.status_code == 413


@pytest.mark.parametrize("path", ["/healthz/", "/livez/", "/metrics/"])
async def test_a_probe_path_with_a_trailing_slash_is_not_refused(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A probe path with a trailing slash is exempt from the credential check.

    Otherwise a kubelet probe on `/healthz/` never goes ready and the log reads as a credential
    problem. This proves only the middleware's exemption; that the route actually answers 200 is
    `test_connector_app.py::test_a_probe_path_with_a_trailing_slash_answers`.
    """
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    assert _is_open(path), "the middleware must not decide a probe path needs a credential"
    response = await _call(_app(token_env=TOKEN_ENV), "GET", path)
    assert response.status_code != 401


async def test_the_open_paths_are_not_a_prefix_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normalising the trailing slash must not become "anything starting with /healthz"."""
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    app = _app(token_env=TOKEN_ENV)
    for path in (
        "/healthz/../mcp",
        "//metrics",
        "/HEALTHZ",
        "/healthzz",
        "/livezz",
        "/LIVEZ",
        "/metrics/../mcp",
    ):
        response = await _call(app, "GET", path)
        assert response.status_code == 401, f"{path} reached the app unauthenticated"
