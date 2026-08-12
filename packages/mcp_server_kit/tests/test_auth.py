"""The credential is checked, the probes stay open, and a misconfigured server serves nothing.

Chemclaw3's incident is the reason each of these exists: `BearerAuth` lived only on the sending
side, so a deployment mounted a secret, recorded the control as enabled, and served every tool to
anything that could reach the pod. The middleware is only real if a test proves the refusal.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from mcp_server_kit.auth import BearerAuthMiddleware, BodySizeLimit

TOKEN_ENV = "TEST_SERVER_TOKEN"


def _app(*, token_env: str | None, max_bytes: int = 0) -> FastAPI:
    """A minimal app carrying the same middleware stack `connector_app` installs."""
    app = FastAPI()
    app.add_middleware(BearerAuthMiddleware, server="test", token_env=token_env)
    if max_bytes:
        app.add_middleware(BodySizeLimit, max_bytes=max_bytes)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/mcp")
    async def mcp() -> dict[str, str]:
        return {"served": "yes"}

    return app


async def _call(app: FastAPI, method: str, path: str, **kwargs: object) -> httpx.Response:
    """Drive the app in-process over ASGI — no socket, so no egress question arises."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)  # type: ignore[arg-type]


async def test_healthz_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A kubelet probe has no identity, and the payload carries nothing worth protecting."""
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    response = await _call(_app(token_env=TOKEN_ENV), "GET", "/healthz")
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

    Sent as raw bytes, because that is how it arrives on the wire: Starlette decodes headers as
    latin-1, so a byte above 0x7F reaches the comparison as a non-ASCII `str`. An httpx `str`
    header would be rejected by the client before it ever left, which would test nothing.
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
