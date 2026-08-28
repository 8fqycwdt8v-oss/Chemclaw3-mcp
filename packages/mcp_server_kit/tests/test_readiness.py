"""`/healthz` on the path it exists for: the check that fails.

Everything about readiness that is worth asserting is about the *failure* — the success path is a
constant 200 with a dataset list, and every server's own suite already covers its corpora. The
failing path is where three things went wrong at once and none of them is visible from the code:

- the 503 body is served on an **unauthenticated** route (`/healthz` is in `auth.OPEN_PATHS`) and
  carried `str(exc)` verbatim, so a loader failure published whatever the loader's message held;
- `lru_cache` does not cache exceptions, so the one path that re-runs work forever is the one a
  failing pod is on, and every probe and every retry re-ran the whole check;
- that work went to `asyncio.to_thread`'s **default** executor, which on `servers/calc` is the same
  pool a tool call offloads a calculation into and which `engine/admission.py`'s ceiling does not
  govern — so a pod proving it cannot answer competed with the calls it could still have answered.

Driven over ASGI rather than a socket: the behaviour under test is entirely inside the route, and
concurrency here has to be real event-loop concurrency, which `httpx.ASGITransport` plus
`asyncio.gather` gives without a port.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp_server_kit.app import READINESS_FAILURE_TTL_SECONDS, connector_app
from mcp_server_kit.datasets import Dataset

# The reason a corpus check gives when it fails, carrying the two things such a message really does
# carry: a filesystem path an operator needs, and a credential nobody may publish.
SECRET = "hunter2"
REASON = (
    f"corpus /opt/app/data/records.csv does not match the approved checksum; PGPASSWORD={SECRET}"
)


class _Recorder:
    """A readiness check that fails, counting its invocations and naming the thread it ran on."""

    def __init__(self) -> None:
        self.calls = 0
        self.threads: set[str] = set()
        self.lock = threading.Lock()

    def __call__(self) -> tuple[Dataset, ...]:
        """Fail the way a bad corpus does, after long enough for a herd to pile up behind it."""
        with self.lock:
            self.calls += 1
            self.threads.add(threading.current_thread().name)
        time.sleep(0.05)
        raise RuntimeError(REASON)


def _client(readiness: _Recorder, *, name: str) -> httpx.AsyncClient:
    """An HTTP client speaking to the real `connector_app` over ASGI, with no credential."""
    app = connector_app(FastMCP(name), name=name, token_env=None, readiness=readiness)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://readiness.test"
    )


async def test_the_503_body_is_redacted_because_the_route_is_unauthenticated() -> None:
    """`/healthz` is open by design, so its body is published to anything that reaches the pod.

    Measured before the fix, with no `Authorization` header at all: the body carried
    `PGPASSWORD=hunter2` while the log line for the very same exception was correctly scrubbed —
    the two records of one fault disagreeing about what may be said, with the *unauthenticated* one
    saying more. `redact_secrets` is exported from `logging.py` for exactly this.
    """
    from mcp_server_kit.logging import redact_secrets

    async with _client(_Recorder(), name="readiness-redaction") as client:
        response = await client.get("/healthz")

    assert response.status_code == 503
    reason = str(response.json()["reason"])
    assert SECRET not in reason, "a credential reached an unauthenticated 503 body"
    assert "/opt/app/data/records.csv" in reason, "the reason must stay diagnostic after redaction"
    assert reason == redact_secrets(REASON), (
        "the body and the log line must be scrubbed by the same function; two spellings of "
        "'scrub a credential' is how one of them goes stale"
    )


async def test_a_herd_of_probes_runs_the_failing_check_once() -> None:
    """The path `lru_cache` cannot help with is the path every probe is on when it matters.

    Measured before: 41 requests produced 41 full readiness invocations and 40 concurrent probes
    peaked at 47 threads against a baseline of 2. A pod that cannot answer must not spend its CPU
    re-proving that; the check is single-flighted and its failure is believed for
    `READINESS_FAILURE_TTL_SECONDS`.
    """
    recorder = _Recorder()
    async with _client(recorder, name="readiness-herd") as client:
        responses = await asyncio.gather(*(client.get("/healthz") for _ in range(40)))

    assert {response.status_code for response in responses} == {503}
    assert recorder.calls == 1, (
        f"40 concurrent probes ran the failing readiness check {recorder.calls} times; every "
        "one of them is a full corpus re-hash on a pod that is already failing"
    )


async def test_the_check_does_not_run_in_the_default_executor() -> None:
    """The pool matters as much as the count, and this is the half a call count cannot show.

    `asyncio.to_thread` uses the interpreter's default executor — on `servers/calc` the same pool
    every tool offloads a minute-long calculation into, and one `engine/admission.py`'s ceiling
    does not govern. A readiness check that blocks there takes a worker away from the calls the pod
    could still be answering, so it gets a single thread of its own, named for the server.
    """
    recorder = _Recorder()
    async with _client(recorder, name="readiness-pool") as client:
        await client.get("/healthz")

    assert recorder.threads and all(
        thread.startswith("readiness-pool-readiness") for thread in recorder.threads
    ), (
        f"the readiness check ran on {recorder.threads!r}; on the default executor it competes "
        "with the tool calls it is reporting on"
    )


async def test_a_recovered_server_is_readied_once_the_memo_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memoising a failure must not outlive the failure, or a fixed pod never becomes ready.

    The window is seconds rather than minutes for this reason; shortened here so the test measures
    the expiry rather than waiting for it.
    """
    monkeypatch.setattr("mcp_server_kit.app.READINESS_FAILURE_TTL_SECONDS", 0.05)
    assert READINESS_FAILURE_TTL_SECONDS > 0
    healthy = Dataset(
        name="probe-corpus",
        version="1",
        licence="CC0",
        retrieved_from="vendored",
        description="a probe",
        sha256="0" * 64,
        records_path=Path("/nonexistent/probe-corpus.csv"),
    )
    broken = True

    def readiness() -> tuple[Dataset, ...]:
        """Fail until `broken` is cleared, the way a pod does when its mount is fixed."""
        if broken:
            raise RuntimeError(REASON)
        return (healthy,)

    app = connector_app(
        FastMCP("readiness-recovery"),
        name="readiness-recovery",
        token_env=None,
        readiness=readiness,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://readiness.test"
    ) as client:
        assert (await client.get("/healthz")).status_code == 503
        broken = False
        assert (await client.get("/healthz")).status_code == 503, "the memo must be believed"
        await asyncio.sleep(0.06)
        recovered = await client.get("/healthz")

    assert recovered.status_code == 200
    assert recovered.json()["datasets"] == ["probe-corpus@1"]
