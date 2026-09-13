"""`/healthz` on the path it exists for: the check that fails.

Everything about readiness that is worth asserting is about the *failure* — the success path is a
constant 200 with a dataset list, and every server's own suite already covers its corpora. The
failing path is where three things went wrong at once and none of them is visible from the code:

- the 503 body is served on an **unauthenticated** route (`/healthz` is in `auth.OPEN_PATHS`) and
  carried `str(exc)` verbatim, so a loader failure published whatever the loader's message held.
  #37 fixed that on `main` while this branch was in flight and its one-line change is the one that
  shipped; what is asserted here is the half a single request cannot see — that the *memo* added
  below holds the scrubbed string too, so a cached 503 and a fresh one cannot say different things.
  `test_connector_app.py::test_readiness_failure_is_a_503_naming_the_reason` is #37's own test and
  covers the same fix over a real socket; both are kept, and reverting the scrub fails both;
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
from mcp_server_kit import degradation
from mcp_server_kit.app import READINESS_FAILURE_TTL_SECONDS, connector_app
from mcp_server_kit.datasets import Dataset
from mcp_server_kit.egress import EgressForbidden
from prometheus_client import REGISTRY

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
    saying more. `redact_secrets` is exported from `logging.py` for exactly this. #37 reached the
    same conclusion independently and its wording is on the branch in `connector_app`.

    **Both the freshly computed 503 and the memoised one, because this route now has two.** The
    memo that keeps a failing pod from re-hashing its corpus per probe is a second place the reason
    string lives, so "scrub what you return" and "scrub what you cache" are two claims and only one
    of them is the interesting one. Measured, with the return scrubbed and the memo holding
    `str(exc)`: the first probe's body was clean and every probe for the next five seconds leaked —
    which the single-request form of this test could not see, because a single request never
    reaches the memo. The invariant is that the two bodies are the same string.
    """
    from mcp_server_kit.logging import redact_secrets

    recorder = _Recorder()
    async with _client(recorder, name="readiness-redaction") as client:
        fresh = await client.get("/healthz")
        memoised = await client.get("/healthz")

    assert (fresh.status_code, memoised.status_code) == (503, 503)
    assert recorder.calls == 1, "the second probe must be the memo, or this test proves half of it"

    for label, response in (("fresh", fresh), ("memoised", memoised)):
        reason = str(response.json()["reason"])
        assert SECRET not in reason, f"a credential reached the {label} unauthenticated 503 body"
        assert "/opt/app/data/records.csv" in reason, "the reason must stay diagnostic"
        assert reason == redact_secrets(REASON), (
            f"the {label} body and the log line must be scrubbed by the same function; two "
            "spellings of 'scrub a credential' is how one of them goes stale"
        )
    assert fresh.json()["reason"] == memoised.json()["reason"], (
        "a cached 503 and a freshly computed one said different things"
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


# One exception per member of `degradation.CAUSES`, as a module constant so the coverage test below
# can read it. A literal list inside the decorator is a list nothing can check against `CAUSES`.
_VERDICT_CASES: dict[str, BaseException] = {
    degradation.CAUSE_EGRESS_REFUSED: EgressForbidden(f"{REASON} (huggingface.co)"),
    degradation.CAUSE_RESOURCE_EXHAUSTED: MemoryError(REASON),
    degradation.CAUSE_NOT_INSTALLED: ImportError(REASON),
    degradation.CAUSE_FAILED: RuntimeError(REASON),
}


@pytest.mark.parametrize("cause", sorted(_VERDICT_CASES), ids=str)
async def test_only_a_permanent_cause_answers_unready(cause: str) -> None:
    """The rule the fleet stated and two callables implemented for one branch each.

    `PERMANENT_CAUSES` was read in exactly **two** places in all of `src/` while all seven servers
    passed a `readiness=` callable, so every other line of every callable raised straight into an
    unconditional 503 — and at the time both probes shared this route, so that 503 was a kill.
    Driven on `rxnlabel` before the funnel: a `MemoryError` out of `RXNMapper()` booked
    `resource_exhausted` on `chemclaw_mcp_degraded_total`, `resource_exhausted in PERMANENT_CAUSES`
    was False, and `/healthz` answered 503 anyway.

    Parametrized over the four causes and asserted against `PERMANENT_CAUSES` membership rather than
    against a transcribed list of status codes: a fifth cause added to `CAUSES` without a decision
    about this route makes `test_every_cause_reaches_this_funnel` red, and moving a cause between
    the two sets flips the expectation here without anybody editing it.
    """
    exc = _VERDICT_CASES[cause]
    expected = 503 if cause in degradation.PERMANENT_CAUSES else 200

    def readiness() -> tuple[Dataset, ...]:
        """Fail the way a component does, with a cause `classify` can see."""
        raise exc

    app = connector_app(
        FastMCP(f"verdict-{cause}"), name=f"verdict-{cause}", token_env=None, readiness=readiness
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://readiness.test"
    ) as client:
        response = await client.get("/healthz")
        alive = await client.get("/livez")

    assert degradation.classify(exc) == cause, "the premise: this exception classifies as stated"
    assert response.status_code == expected, (
        f"a {cause} readiness failure answered {response.status_code}; the rule is that only "
        f"{sorted(degradation.PERMANENT_CAUSES)} may take a pod out of rotation"
    )
    body = response.json()
    if expected == 200:
        assert body["degraded"] == cause, "a pod left in service must say what it could not verify"
        assert "datasets" not in body, "nothing was verified, so nothing may be claimed as verified"
    else:
        assert body["status"] == "unready" and REASON.split(";")[0] in body["reason"]
    assert alive.status_code == 200, (
        "liveness must not read readiness: a 503 here would kill the pod rather than shed its "
        "traffic, which is what sharing one route did"
    )


def test_every_cause_reaches_this_funnel() -> None:
    """A fifth cause must not be able to arrive with no decision about what the probe answers.

    The parametrization above is a list, and a list is exactly the thing that silently stops
    covering its subject. This asserts the list *is* `CAUSES`.
    """
    covered = frozenset(_VERDICT_CASES)
    assert covered == degradation.CAUSES, (
        f"the verdict test covers {sorted(covered)} of {sorted(degradation.CAUSES)}; a cause with "
        "no case here is a cause whose effect on a kubelet probe nobody decided"
    )


async def test_the_transient_verdict_is_counted_for_a_scrape() -> None:
    """Leaving a pod in service is only defensible if the degradation is visible somewhere."""
    labels = {
        "server": "readiness-counted",
        "component": "readiness",
        "cause": degradation.CAUSE_RESOURCE_EXHAUSTED,
    }
    before = REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels) or 0.0

    def readiness() -> tuple[Dataset, ...]:
        """Run out of memory, the way a transformer probe does under pressure."""
        raise MemoryError("Unable to allocate 48.0 MiB for an array")

    app = connector_app(
        FastMCP("readiness-counted"),
        name="readiness-counted",
        token_env=None,
        readiness=readiness,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://readiness.test"
    ) as client:
        assert (await client.get("/healthz")).status_code == 200

    assert REGISTRY.get_sample_value("chemclaw_mcp_degraded_total", labels) == before + 1.0


async def test_livez_answers_while_healthz_refuses() -> None:
    """The decoupling, driven: the two routes disagree, which is the whole point of there being two.

    Before this, one 503 meant both "stop sending me traffic" and "replace me" — `periodSeconds: 30`
    and `failureThreshold: 3`, so ~90 s. `/livez` must therefore be reachable, answer 200, and touch
    nothing the readiness check touches: the recorder below counts its invocations, and a `/livez`
    that consulted readiness would both raise and move that count.
    """
    recorder = _Recorder()
    async with _client(recorder, name="readiness-livez") as client:
        unready = await client.get("/healthz")
        alive = await client.get("/livez")
        aliased = await client.get("/livez/", follow_redirects=False)

    assert unready.status_code == 503, "the premise: this pod is not ready"
    assert alive.status_code == 200 and alive.json()["status"] == "alive"
    assert aliased.status_code == 200, (
        "a kubelet probe written `path: /livez/` must answer the probe itself, not a 307 or a 404 "
        "from the MCP mount — kubelet counts a 3xx as a pass, so a redirect would report a dead "
        "pod alive"
    )
    assert recorder.calls == 1, (
        f"the readiness check ran {recorder.calls} times for one /healthz and two /livez calls; a "
        "liveness probe that runs the readiness check is the coupling this route exists to remove"
    )
