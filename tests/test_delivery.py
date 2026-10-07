"""The delivery pipeline describes this fleet; these are the halves a file can check.

`Jenkinsfile` cannot run here, so its claims about this tree are checked, chiefly that it
derives the server list from the filesystem: a written list fails open, since a server nobody
builds is silently never deployed. Every workflow under `.github/workflows/` is read too,
because a suite job on a shallow checkout would disable the reachability check. Whether any of
it works against a registry is not checked.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
JENKINSFILE = ROOT / "Jenkinsfile"
WORKFLOWS = ROOT / ".github" / "workflows"
SERVERS = ROOT / "servers"


def _pipeline() -> str:
    return JENKINSFILE.read_text(encoding="utf-8")


def test_the_pipeline_derives_its_server_list_from_the_tree() -> None:
    """A list written here is a list that is wrong by the next server."""
    text = _pipeline()
    assert "ls -d servers/*/Containerfile" in text, (
        "the pipeline no longer discovers servers from the tree; a hand-kept list fails open — "
        "an unbuilt server is an undeployed one, with nothing red to say so"
    )
    names = sorted(path.parent.name for path in SERVERS.glob("*/Containerfile"))
    assert names, "no server Containerfiles found — this test would assert nothing"
    # A discovery step plus an enumeration is the worst of both: the list looks authoritative and
    # silently shadows what was discovered.
    hardcoded = [name for name in names if re.search(rf"defaultValue:\s*'[^']*\b{name}\b", text)]
    assert not hardcoded, f"server names hardcoded into a pipeline default: {hardcoded}"


def test_every_server_with_a_containerfile_can_be_addressed_by_the_pipeline() -> None:
    """The pipeline builds `servers/<name>/Containerfile` and tags `chemclaw-mcp-<name>`.

    Both halves of that convention are load-bearing: the image name is what a Chemclaw3 release
    descriptor names, so a server that broke the pattern would build here and be undeployable there.
    """
    assert "servers/${name}/Containerfile" in _pipeline()
    assert "chemclaw-mcp-${name}" in _pipeline()
    for server in sorted(SERVERS.glob("*/Containerfile")):
        assert (server.parent / "connector.yaml").is_file(), (
            f"{server.parent.name} has an image and no manifest; the pipeline reads its port and "
            f"credential env out of the manifest and would build something it cannot verify"
        )


def test_the_running_image_is_verified_rather_than_the_source() -> None:
    """The pipeline verifies the started container, not the source, for two facts.

    The revision reaching `/healthz` needs the build argument actually set, and bearer enforcement
    on the mounted `/mcp` surface cannot be read off the source.
    """
    text = _pipeline()
    assert "/healthz" in text and "REVISION" in text, "the built image's revision is not checked"
    assert re.search(r"401\|403", text), "an unauthenticated /mcp call is not proven to be refused"


def test_the_publish_path_reports_digests_rather_than_tags() -> None:
    """A tag is a pointer; a deployment that follows one cannot be rolled back to known bytes."""
    text = _pipeline()
    assert "build_and_push" in text, "the publish path no longer returns the registry's digest"
    assert "mcp-digests.txt" in text, "nothing carries the digests to the release job"


def test_dry_run_is_the_default() -> None:
    """First runs happen against real registries."""
    assert "booleanParam(name: 'DRY_RUN', defaultValue: true" in _pipeline()


def test_a_publishing_run_cannot_skip_the_gate() -> None:
    """A publishing run cannot skip the gate.

    `RUN_GATE` defaults off and the pipeline cannot see GitHub Actions, so Preflight must refuse.
    Read as source (driving needs a Jenkins), three facts: the refusal names all three conditions
    (`!DRY_RUN`, a registry, `!RUN_GATE`), so a build-only run is not refused; it `error`s rather
    than warns; and the `Gate` stage it points at still runs `make check`.
    """
    text = _pipeline()
    guard = "if (!params.DRY_RUN && env.IMAGE_REGISTRY && !params.RUN_GATE) {"
    assert guard in text, (
        "the Preflight stage does not refuse a publishing run with RUN_GATE off; an image can "
        "again be published from a revision this pipeline never gated"
    )
    after = text.split(guard, 1)[1]
    assert after.lstrip().startswith("error("), (
        "the guard does not `error`: a warning is indistinguishable from no guard by the time the "
        "digest has been pushed"
    )
    assert "when { expression { params.RUN_GATE } }" in text and "sh 'make check'" in text, (
        "the refusal points operators at a `Gate` stage that no longer runs `make check`"
    )


def test_the_pipeline_has_one_answer_to_whether_there_is_a_registry() -> None:
    """The pipeline has one answer to whether there is a registry.

    Groovy treats a whitespace-only string as truthy while its `trim()` is not, so different
    spellings of the predicate disagree. The pipeline trims once into `env.IMAGE_REGISTRY`; this
    asserts `params.IMAGE_REGISTRY` is read exactly once, where it is normalised.
    """
    # The comment above the assignment quotes the old spellings, so every assertion below reads the
    # comment-stripped code.
    code = "\n".join(
        line for line in _pipeline().splitlines() if not line.lstrip().startswith("//")
    )
    assert "env.IMAGE_REGISTRY = params.IMAGE_REGISTRY?.trim() ?: ''" in code, (
        "the pipeline no longer normalises IMAGE_REGISTRY once in Preflight; every later read is "
        "then free to disagree about whether a whitespace-only value is a registry"
    )
    reads = code.count("params.IMAGE_REGISTRY")
    assert reads == 1, (
        f"`params.IMAGE_REGISTRY` is read {reads} times in the pipeline's code; it may be read "
        "only where it is trimmed into `env.IMAGE_REGISTRY`, so that one answer decides both the "
        "Preflight refusal and the publish branch"
    )


def _shell_as_the_shell_receives_it(block: str) -> str:
    r"""Resolve a Groovy GString to the text bash is actually handed.

    Jenkins interpolates an unescaped dollar-brace before the shell sees it, while an escaped dollar
    reaches the shell verbatim; getting that backwards is the commonest way these files break.
    """
    resolved = re.sub(r"(?<!\\)\$\{[^}]*\}", "PLACEHOLDER", block)
    return resolved.replace("\\$", "$").replace("\\\\", "\\")


def test_every_shell_block_in_the_pipeline_parses() -> None:
    """Every shell block in the pipeline parses under `bash -n`.

    No compiler or linter here reads a Jenkinsfile, and its shell bodies are strings, so an
    unbalanced quote would otherwise surface only in a run.
    """
    text = _pipeline()
    blocks = re.findall(r'"""(.*?)"""', text, re.S) + re.findall(r"sh '''(.*?)'''", text, re.S)
    assert len(blocks) >= 3, f"only {len(blocks)} shell blocks found — the parse has drifted"
    for block in blocks:
        script = _shell_as_the_shell_receives_it(block)
        result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
        assert result.returncode == 0, f"a shell block does not parse: {result.stderr.strip()}"


# The commands that run this repository's suite. A job running one also runs the commit
# reachability check, which needs full history: in a shallow clone it warns and returns instead of
# failing, and `actions/checkout` defaults to depth 1. The bare `pytest` runner is listed because
# a job may invoke it directly rather than through `make`.
_SUITE_COMMANDS = (
    "make cov",
    "make test",
    "make check",
    "make offline-run",
    "offline_check.py",
    "pytest",
)


def _ci_jobs() -> dict[str, dict[str, Any]]:
    """Every job in every GitHub Actions workflow, keyed `<file>:<job>`.

    Every workflow file, not only `ci.yml`, since a new file is the cheapest way to add a job. The
    key carries the filename so a failure says which file to open. `Any` for the value because a job
    is a free-form YAML mapping the callers index into.
    """
    files = sorted(path for path in WORKFLOWS.glob("*.y*ml") if path.suffix in {".yml", ".yaml"})
    assert files, f"no workflow files under {WORKFLOWS}; this parse would assert nothing"
    jobs: dict[str, dict[str, Any]] = {}
    for path in files:
        declared = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]
        assert isinstance(declared, dict) and declared, f"{path.name} declares no jobs"
        jobs.update({f"{path.name}:{name}": job for name, job in declared.items()})
    return jobs


def test_every_job_that_runs_the_suite_checks_out_full_history() -> None:
    """Every job that runs the suite checks out full history (`fetch-depth: 0`).

    At the default depth the reachability check does not fail, it does not run, and a stranded hash
    would leave CI green while `main` is red with full history. Workflows are read like
    `Jenkinsfile` for the same reason: nothing else checks them. Jobs are derived from every
    workflow file; what a job runs is matched against `_SUITE_COMMANDS`, a list of spellings.
    Jenkins' `Gate` checkout depth is controller configuration and out of reach.
    """
    running = {
        name: job
        for name, job in _ci_jobs().items()
        if any(
            command in str(step.get("run", ""))
            for step in job.get("steps", [])
            for command in _SUITE_COMMANDS
        )
    }
    assert running, f"no job under {WORKFLOWS} runs the suite — this test would assert nothing"
    for name, job in running.items():
        checkouts = [
            step for step in job.get("steps", []) if "actions/checkout" in str(step.get("uses", ""))
        ]
        assert checkouts, (
            f"job {name!r} runs the suite with no `actions/checkout` step; the commit-reachability "
            "assertion cannot read ancestry it was never given"
        )
        for step in checkouts:
            depth = (step.get("with") or {}).get("fetch-depth")
            assert depth == 0, (
                f"job {name!r} checks out with fetch-depth={depth!r}; `actions/checkout` defaults "
                "to depth 1, where test_every_commit_the_registers_cite_is_reachable_from_head "
                "warns and returns instead of failing — so the citations go unchecked in CI while "
                "the run stays green"
            )


def test_ci_builds_every_image_from_a_list_it_discovers() -> None:
    """A workflow builds every server's image, from a server list it discovers in the tree.

    The suite reads a Containerfile only as text, so only a build catches a bake step that fails; a
    written list would miss the next server and fail open.
    """
    jobs = _ci_jobs()
    builders = {
        name: job
        for name, job in jobs.items()
        if any("docker build" in str(step.get("run", "")) for step in job.get("steps", []))
    }
    assert builders, f"no job under {WORKFLOWS} builds an image; a Containerfile is only ever read"
    names = sorted(path.parent.name for path in SERVERS.glob("*/Containerfile"))
    for name, job in builders.items():
        matrix = str((job.get("strategy") or {}).get("matrix", ""))
        assert "fromJSON" in matrix, f"job {name!r} takes no discovered list of servers"
        written = [server for server in names if re.search(rf"\b{server}\b", matrix)]
        assert not written, f"job {name!r} names servers in its matrix: {written}"
    discovery = "\n".join(
        str(step.get("run", "")) for job in jobs.values() for step in job.get("steps", [])
    )
    assert "servers/*/Containerfile" in discovery, "no job discovers servers from the tree"
    # The narrowing to touched servers is what let `chem` and `calc` ship images that never
    # started: the job's own pull request built two servers and proved nothing about the rest. A
    # change to the harness, or to a base image, is a claim about every image, so both widen.
    for trigger in (r"\.github/workflows/", "FROM"):
        assert trigger in discovery, (
            f"image discovery no longer widens to every server on {trigger}"
        )
