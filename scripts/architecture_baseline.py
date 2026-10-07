"""Measure the fleet's architecture baseline into `docs/architecture-baseline.json`.

Three numbers per server, taken from the working tree: the cold import time of its `app` module, a
`/healthz` round trip, and an MCP `initialize` + `list_tools` round trip against the real app under
uvicorn on loopback. For `calc` it also times one `calculation_key` call, the cache-hit path
Chemclaw3 pays on every lookup. Plus the prose share of non-test source (docstring and comment
lines over non-blank lines) twice: read from a git revision (`--rev`, default the pre-programme
commit `BASELINE_REV`) so the baseline stays fixed however often this is re-run, and from the
working tree so the current share sits beside it. The file name carries no date, so a re-run
replaces it rather than adding a second baseline beside it.

Each server is measured in its own child process so one server's imports never warm another's.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import datetime
import io
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import tokenize
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROUND_TRIPS = 5
# The last commit before the architecture programme's prose diet; the baseline prose share is read
# here so a re-run measures the same thing.
BASELINE_REV = "4af1fdc"
SERVER_TIMEOUT_S = 600


def _free_port() -> int:
    """An ephemeral loopback port, released for uvicorn to claim."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _ms(seconds: float) -> float:
    """Seconds to milliseconds, rounded for a readable JSON file."""
    return round(seconds * 1000.0, 2)


async def _mcp_round_trips(base: str, token: str, name: str) -> dict[str, Any]:
    """Time `initialize` + `list_tools` (and `calculation_key` on calc) over a real session."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"}
    handshakes: list[float] = []
    keys: list[float] = []
    tool_count = 0
    for _ in range(ROUND_TRIPS):
        started = time.perf_counter()
        async with (
            httpx.AsyncClient(headers=headers) as http_client,
            streamable_http_client(f"{base}/mcp", http_client=http_client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            handshakes.append(time.perf_counter() - started)
            tool_count = len(listed.tools)
            if name == "calc":
                arguments = {"tool": "compute_xtb_energy", "arguments": {"smiles": "CCO"}}
                key_started = time.perf_counter()
                result = await session.call_tool("calculation_key", arguments)
                keys.append(time.perf_counter() - key_started)
                if result.isError:
                    raise RuntimeError(f"calculation_key failed: {result.content}")
    measured: dict[str, Any] = {
        "tools": tool_count,
        "mcp_initialize_list_tools_ms_median": _ms(statistics.median(handshakes)),
        "mcp_initialize_list_tools_ms_first": _ms(handshakes[0]),
    }
    if keys:
        measured["calculation_key_ms_median"] = _ms(statistics.median(keys))
    return measured


def measure_one(name: str) -> dict[str, Any]:
    """Import one server's app cold, serve it on loopback, and time the probe and the handshake."""
    import httpx
    import uvicorn

    manifest = yaml.safe_load((ROOT / "servers" / name / "connector.yaml").read_text())
    token_env = manifest["endpoint"]["auth"]["token_env"]
    token = "architecture-baseline-token"  # noqa: S105 - a throwaway loopback credential
    os.environ[token_env] = token

    started = time.perf_counter()
    module = __import__(f"chemclaw_mcp_{name}.app", fromlist=["app"])
    import_s = time.perf_counter() - started

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(module.app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError(f"{name} did not start within 60 s")
        time.sleep(0.02)
    try:
        probes: list[float] = []
        status = 0
        with httpx.Client(timeout=30.0) as client:
            for _ in range(ROUND_TRIPS):
                probe_started = time.perf_counter()
                status = client.get(f"{base}/healthz").status_code
                probes.append(time.perf_counter() - probe_started)
        result: dict[str, Any] = {
            "cold_import_ms": _ms(import_s),
            "healthz_status": status,
            "healthz_ms_first": _ms(probes[0]),
            "healthz_ms_median": _ms(statistics.median(probes)),
        }
        result.update(asyncio.run(_mcp_round_trips(base, token, name)))
        return result
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def measure_servers() -> dict[str, Any]:
    """Every server, each in a fresh interpreter so imports are cold."""
    results: dict[str, Any] = {}
    servers = sorted(
        p.name for p in (ROOT / "servers").iterdir() if (p / "connector.yaml").is_file()
    )
    for name in servers:
        try:
            completed = subprocess.run(
                [sys.executable, __file__, "--one", name],
                capture_output=True,
                text=True,
                timeout=SERVER_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            results[name] = {"error": f"timed out after {SERVER_TIMEOUT_S} s"}
            continue
        lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
        if completed.returncode == 0 and lines:
            results[name] = json.loads(lines[-1])
        else:
            tail = (completed.stderr or completed.stdout).strip().splitlines()[-3:]
            results[name] = {"error": " | ".join(tail)}
    return results


def prose_lines(source: str) -> tuple[int, int]:
    """Return (prose lines, non-blank lines): docstrings plus comment-only lines, over non-blank."""
    lines = source.splitlines()
    prose: set[int] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for statement in body:
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
                and statement.end_lineno is not None
            ):
                prose.update(range(statement.lineno, statement.end_lineno + 1))
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            row = token.start[0]
            if lines[row - 1].strip().startswith("#"):
                prose.add(row)
    non_blank = {number for number, line in enumerate(lines, 1) if line.strip()}
    return len(prose & non_blank), len(non_blank)


def _share(prose: int, total: int) -> float:
    """`prose / total`, or 0.0 for a bucket with no non-blank lines."""
    return round(prose / total, 4) if total else 0.0


def _tally(sources: dict[str, str]) -> dict[str, Any]:
    """Prose share of `{path: source}` for every non-test `.py`, per package and in total."""
    per_package: dict[str, list[int]] = {}
    for path, source in sources.items():
        parts = path.split("/")
        key = "/".join(parts[:2]) if parts[0] in {"servers", "packages"} else parts[0]
        prose, total = prose_lines(source)
        bucket = per_package.setdefault(key, [0, 0])
        bucket[0] += prose
        bucket[1] += total
    prose_total = sum(p for p, _ in per_package.values())
    lines_total = sum(t for _, t in per_package.values())
    return {
        "total": {
            "prose_lines": prose_total,
            "non_blank_lines": lines_total,
            "share": _share(prose_total, lines_total),
        },
        "per_package": {
            key: {"prose_lines": p, "non_blank_lines": t, "share": _share(p, t)}
            for key, (p, t) in sorted(per_package.items())
        },
    }


def _git(*args: str) -> str:
    """Run a read-only git command in the repository and return its stdout."""
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def _is_counted(path: str) -> bool:
    """A non-test Python source file."""
    return path.endswith(".py") and "tests" not in path.split("/")


def prose_share(rev: str) -> dict[str, Any]:
    """Prose share of every tracked non-test `.py` at `rev`."""
    paths = [
        path for path in _git("ls-tree", "-r", "--name-only", rev).split() if _is_counted(path)
    ]
    sources = {path: _git("show", f"{rev}:{path}") for path in paths}
    return {"rev": _git("rev-parse", "--short", rev).strip(), **_tally(sources)}


def prose_share_working_tree() -> dict[str, Any]:
    """Prose share of every tracked non-test `.py` as it stands on disk now."""
    paths = [path for path in _git("ls-files").split() if _is_counted(path)]
    sources = {
        path: (ROOT / path).read_text(encoding="utf-8") for path in paths if (ROOT / path).is_file()
    }
    return {"rev": "working tree", **_tally(sources)}


def main() -> int:
    """Measure, or measure one server when called as a child."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--one", help="measure one server and print its JSON (internal)")
    parser.add_argument(
        "--rev", default=BASELINE_REV, help="git revision the baseline prose share is read at"
    )
    parser.add_argument(
        "--out", type=Path, help="output path (default: docs/architecture-baseline.json)"
    )
    args = parser.parse_args()
    if args.one:
        print(json.dumps(measure_one(args.one)))
        return 0
    report = {
        "date": datetime.date.today().isoformat(),
        "python": sys.version.split()[0],
        "round_trips": ROUND_TRIPS,
        "servers": measure_servers(),
        "prose": prose_share(args.rev),
        "prose_working_tree": prose_share_working_tree(),
    }
    out = args.out or ROOT / "docs" / "architecture-baseline.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
