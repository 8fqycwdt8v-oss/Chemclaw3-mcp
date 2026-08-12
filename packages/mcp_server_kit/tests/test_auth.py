"""The credential is checked, the probes stay open, and a misconfigured server serves nothing.

Chemclaw3's incident is the reason each of these exists: `BearerAuth` lived only on the sending
side, so a deployment mounted a secret, recorded the control as enabled, and served every tool to
anything that could reach the pod. The middleware is only real if a test proves the refusal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from mcp_server_kit.auth import BearerAuthMiddleware, BodySizeLimit, CallerLogMiddleware

TOKEN_ENV = "TEST_SERVER_TOKEN"


def _app(*, token_env: str | None, max_bytes: int = 0) -> FastAPI:
    """A minimal app carrying the same middleware stack `connector_app` installs.

    The order matters and is copied deliberately: two `BaseHTTPMiddleware` layers sit between the
    body cap and the route, and each of them runs what it wraps in an anyio task group. That
    nesting is what wraps the oversize sentinel on its way back out.
    """
    app = FastAPI()
    app.add_middleware(CallerLogMiddleware, server="test")
    app.add_middleware(BearerAuthMiddleware, server="test", token_env=token_env)
    if max_bytes:
        app.add_middleware(BodySizeLimit, server="test", max_bytes=max_bytes)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/mcp")
    async def mcp() -> dict[str, str]:
        return {"served": "yes"}

    @app.post("/drain")
    async def drain(request: Request) -> dict[str, int]:
        """Reads the raw stream, the way the mounted MCP transport does.

        The `/mcp` route above never pulls from the receive channel, and FastAPI's own body parsing
        swallows what a counting channel raises and answers 400. Neither shape is what a real server
        does with a request body, so the streaming half of the cap needs a route that reads it.
        """
        return {"read": len(await request.body())}

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


async def test_an_oversized_chunked_body_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The half the test above cannot reach: no `content-length`, so only the counter can refuse.

    httpx sets `content-length` for any body it can measure, so the test above exercises the
    declared-length check alone — which is how this cap shipped twice in a row with one of its two
    halves unproven. An async generator body has no length to declare, so the running total is the
    only thing between the request and the route, and the sentinel it raises comes back out through
    two task groups.
    """
    monkeypatch.setenv(TOKEN_ENV, "s3cret")

    async def oversized() -> AsyncIterator[bytes]:
        for _ in range(8):
            yield b"x" * 32  # 256 bytes, no content-length

    response = await _call(
        _app(token_env=TOKEN_ENV, max_bytes=64),
        "POST",
        "/drain",
        headers={"authorization": "Bearer s3cret"},
        content=oversized(),
    )
    assert response.status_code == 413


async def test_a_chunked_body_inside_the_cap_is_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction, or a cap that refused everything would pass the test above."""
    monkeypatch.setenv(TOKEN_ENV, "s3cret")

    async def small() -> AsyncIterator[bytes]:
        yield b"x" * 16

    response = await _call(
        _app(token_env=TOKEN_ENV, max_bytes=64),
        "POST",
        "/drain",
        headers={"authorization": "Bearer s3cret"},
        content=small(),
    )
    assert response.status_code == 200
    assert response.json() == {"read": 16}
