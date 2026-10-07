"""Images: every Containerfile installs exactly what `uv.lock` hashed, builds wheels under the
locked backend, carries its data and its revision, and pins torch's thread width.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


SERVERS = ROOT / "servers"


def server_dirs() -> list[Path]:
    """Every server directory — a subdirectory of `servers/` holding a `connector.yaml`."""
    return sorted(path for path in SERVERS.iterdir() if (path / "connector.yaml").is_file())


def test_every_server_builds_a_wheel_that_carries_its_data() -> None:
    """A server that cannot be packaged cannot be deployed, and nothing else here would notice."""
    import tempfile
    import zipfile

    for server in sorted(p for p in SERVERS.iterdir() if (p / "pyproject.toml").is_file()):
        data_dir = next(iter((server / "src").glob("*/data")), None)
        with tempfile.TemporaryDirectory() as out:
            built = subprocess.run(
                ["uv", "build", "--wheel", "--offline", "--out-dir", out, str(server)],
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            assert built.returncode == 0, (
                f"{server.name} cannot be packaged, so it cannot be deployed:\n{built.stderr}"
            )
            wheels = list(Path(out).glob("*.whl"))
            assert len(wheels) == 1, f"{server.name} built {len(wheels)} wheels"
            names = zipfile.ZipFile(wheels[0]).namelist()

        if data_dir is None:
            continue
        on_disk = sum(1 for p in data_dir.rglob("*") if p.is_file())
        in_wheel = sum(1 for n in names if "/data/" in n)
        assert in_wheel == on_disk, (
            f"{server.name} ships {in_wheel} of its {on_disk} data files; a server whose corpus "
            "is missing answers 'nothing matched' for every input, which reads as a clean result"
        )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_the_image_can_name_the_build_it_is(server: Path) -> None:
    """Every Containerfile threads a build argument into `MCP_SERVER_REVISION`."""
    text = (server / "Containerfile").read_text(encoding="utf-8")
    assert "ARG CHEMCLAW_REVISION" in text, (
        f"{server.name}/Containerfile declares no CHEMCLAW_REVISION build argument, so every "
        "image it builds reports its revision as 'unknown'"
    )
    assert "ENV MCP_SERVER_REVISION=${CHEMCLAW_REVISION}" in text, (
        f"{server.name}/Containerfile takes a revision argument and never puts it in the "
        "environment, which is the only place the server reads it"
    )


def test_the_revision_reaches_the_handshake_and_the_probe() -> None:
    """The revision an image supplies is the one a client handshake and the probe see.

    A claim about `connector_app`, so checked once here. It reaches through the private
    `FastMCP._mcp_server` deliberately (`FastMCP` takes no `version`), so an upstream rename fails
    this rather than silently reporting the SDK's own version.
    """
    from fastapi.testclient import TestClient
    from mcp.server.fastmcp import FastMCP
    from mcp_server_kit.app import connector_app

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MCP_SERVER_REVISION", "abc1234")
        served = FastMCP("probe")
        app = connector_app(served, name="probe")
        options = served._mcp_server.create_initialization_options()
        with TestClient(app) as client:
            probed = client.get("/healthz").json()

    assert options.server_version == "abc1234", (
        "the initialize() handshake reports "
        f"{options.server_version!r}; a client cannot tell which build answered"
    )
    # `bounds` is the process's own record and is asserted where it is produced
    # (`test_healthz_reports_the_bounds_the_process_is_running_with`); here only its presence.
    assert isinstance(probed.pop("bounds"), dict)
    assert probed == {"status": "ok", "server": "probe", "revision": "abc1234"}

    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("MCP_SERVER_REVISION", raising=False)
        bare = FastMCP("probe")
        connector_app(bare, name="probe")
        assert bare._mcp_server.create_initialization_options().server_version == "unknown", (
            "an unstamped build must say so rather than report the MCP SDK's version, which is a "
            "true fact about the wrong thing"
        )


# A version specifier written as a literal inside a Containerfile's `pip install`, e.g.
# `"rxnmapper==0.4.3"`. Quoted because that is how a shell-safe specifier is written; the operator
# is captured so a floating one can be named in the failure rather than merely rejected.
_PIP_SPECIFIER = re.compile(r'"([A-Za-z0-9][A-Za-z0-9._-]*)(==|>=|<=|~=|>|<)([0-9][^"]*)"')


def containerfile_instructions(text: str) -> list[str]:
    """A Containerfile's instructions as logical lines — comments removed, continuations joined."""
    instructions: list[str] = []
    current = ""
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        line = raw.rstrip()
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        current += line
        if current.strip():
            instructions.append(" ".join(current.split()))
        current = ""
    if current.strip():
        instructions.append(" ".join(current.split()))
    return instructions


def _locked_versions() -> dict[str, str]:
    """Every distribution `uv.lock` resolves, name to version — the set `make deps-audit` reads."""
    import tomllib

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = lock["package"]
    assert isinstance(packages, list)
    return {str(entry["name"]): str(entry["version"]) for entry in packages}


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_an_image_that_installs_from_the_index_pins_what_the_audit_read(server: Path) -> None:
    """An image's direct install of a locked package is pinned to the version the audit read."""
    text = "\n".join(containerfile_instructions((server / "Containerfile").read_text("utf-8")))
    locked = _locked_versions()
    for name, operator, version in _PIP_SPECIFIER.findall(text):
        if name.lower().replace("_", "-") not in locked:
            continue  # not something `uv.lock` resolves, so this audit has nothing to say about it
        expected = locked[name.lower().replace("_", "-")]
        assert operator == "==", (
            f"{server.name}/Containerfile installs {name}{operator}{version} from the index: an "
            "open bound re-resolves on every build, so what ships is not what `make deps-audit` "
            f"read. Pin it to =={expected}, the version in uv.lock"
        )
        assert version == expected, (
            f"{server.name}/Containerfile pins {name}=={version} while uv.lock resolves "
            f"{expected}: the audited version and the shipped version have drifted apart"
        )


# The one unhashed `pip install` allowed in a build stage: pip and `uv` themselves, in a discarded
# stage, with `--frozen` keeping resolution fixed whichever `uv` runs. Matched whole, so a package
# appended to it is a new unhashed install and fails.
_BOOTSTRAP_INSTALL = 'python -m pip install --no-cache-dir --upgrade pip "uv>=0.8.17,<1"'


# Flags that point pip at an index other than the default, or at none it can verify. Any of them in
# an image is a resolution the lock did not make, whatever else the line carries — with the one
# exception `_names_only_locked_indexes` spells out.
_INDEX_FLAGS = ("--index-url", "--extra-index-url", "--trusted-host", "-i")


def _locked_registries() -> frozenset[str]:
    """Every package index `uv.lock` records as the source of something it resolved."""
    import tomllib

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return frozenset(
        str(entry["source"]["registry"])
        for entry in lock["package"]
        if "registry" in entry.get("source", {})
    )


def _index_flags(command: str) -> list[tuple[str, str]]:
    """Every `(flag, value)` among `_INDEX_FLAGS` in one pip command, `=`-joined or spaced."""
    tokens = [token.strip("\"'") for token in command.split()]
    found: list[tuple[str, str]] = []
    for position, token in enumerate(tokens):
        flag, joined, value = token.partition("=")
        if flag not in _INDEX_FLAGS:
            continue
        if not joined:
            value = tokens[position + 1] if position + 1 < len(tokens) else ""
        found.append((flag, value))
    return found


def _names_only_locked_indexes(command: str) -> bool:
    """Whether every index flag in a hashed pass is an *extra* index `uv.lock` names as a source."""
    flags = _index_flags(command)
    hashed = "--require-hashes" in command and re.search(r"\s-r\s+\S", command) is not None
    locked = _locked_registries()
    return hashed and all(
        flag == "--extra-index-url" and value.rstrip("/") in {r.rstrip("/") for r in locked}
        for flag, value in flags
    )


def unhashed_installs(instructions: list[str]) -> list[str]:
    """Every `pip install`/`pip wheel` in `instructions` that could fetch something `uv.lock` did
    not hash.
    """
    offending: list[str] = []
    for instruction in instructions:
        if not instruction.startswith("RUN "):
            continue
        for raw in re.split(r"&&|;", instruction[len("RUN ") :]):
            command = " ".join(raw.split())
            if not re.search(r"\bpip3? (install|wheel|download)\b", command):
                continue
            if (
                _index_flags(command) and not _names_only_locked_indexes(command)
            ) or not _fetches_nothing_unhashed(command):
                offending.append(command)
    return offending


def _fetches_nothing_unhashed(command: str) -> bool:
    """Whether one pip command is one of the accepted shapes `unhashed_installs` lists."""
    hashed = "--require-hashes" in command and re.search(r"\s-r\s+\S", command) is not None
    local = "--no-deps" in command and all(t.startswith("./") for t in _pip_targets(command))
    return command == _BOOTSTRAP_INSTALL or hashed or "--no-index" in command or local


# pip options that consume the next token as their value, so it is not a requirement.
_PIP_VALUED = {
    "--extra-index-url",
    "--index-url",
    "-i",
    "--trusted-host",
    "-w",
    "--wheel-dir",
    "-f",
    "--find-links",
    "-r",
    "--requirement",
    "-c",
    "--constraint",
}


def _pip_targets(command: str) -> list[str]:
    """The requirement arguments of one `pip install`/`pip wheel` command, quotes removed."""
    tokens = command.split()
    verb = next(i for i, token in enumerate(tokens) if token in {"install", "wheel", "download"})
    targets: list[str] = []
    skip = False
    for token in tokens[verb + 1 :]:
        if skip:
            skip = False
        elif token in _PIP_VALUED:
            skip = True
        elif not token.startswith("-"):
            targets.append(token.strip("\"'"))
    return targets


#: The distributions PyPI's linux torch wheel depends on and a CPU-only pod never loads.
_CUDA_RUNTIME = re.compile(r"^(nvidia-|cuda-|triton$)")


def test_the_lock_resolves_a_cpu_torch_for_the_cpu_only_pods() -> None:
    """Linux torch comes from PyTorch's CPU index, and no CUDA runtime wheel is in the lock at all.

    Every pod is CPU-only, and PyPI's linux torch pulls gigabytes of CUDA wheels. The index source
    binds only where a package names torch directly, so a transitive path or an edited source would
    re-lock CUDA unseen; the lock is read because it is what images install.
    """
    import tomllib

    packages = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    cuda = sorted(str(entry["name"]) for entry in packages if _CUDA_RUNTIME.match(entry["name"]))
    assert not cuda, (
        f"uv.lock resolves the CUDA runtime again: {cuda}. Some package reached PyPI's linux torch "
        "— name torch directly in its extra so the root `pytorch-cpu` source binds, and re-lock"
    )
    torches = [entry for entry in packages if entry["name"] == "torch"]
    assert torches, "uv.lock resolves no torch at all — this test would assert nothing"
    linux = [entry for entry in torches if str(entry["version"]).endswith("+cpu")]
    assert linux and all(
        entry["source"].get("registry") == "https://download.pytorch.org/whl/cpu" for entry in linux
    ), f"no +cpu torch from the CPU index in uv.lock: {[e['version'] for e in torches]}"


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_no_image_installs_what_the_lock_did_not_hash(server: Path) -> None:
    """Every package an image installs came through a `--require-hashes` pass over the lock."""
    instructions = containerfile_instructions(
        (server / "Containerfile").read_text(encoding="utf-8")
    )
    offending = unhashed_installs(instructions)
    assert not offending, (
        f"{server.name}/Containerfile installs from the index outside the hashed lock export: "
        f"{offending!r}. A version pin is not a hash, and its dependencies re-resolve on the day "
        "of the build — add the package to the server's dependencies or an extra, `uv lock`, and "
        "let the `--require-hashes` pass fetch it"
    )


def test_the_unhashed_install_check_refuses_the_shapes_it_was_written_for() -> None:
    """The unhashed-install check refuses the shapes it was written for, and passes the real ones.

    Driven on synthetic instructions in both directions, so `unhashed_installs` cannot be weakened
    to return nothing while the parametrized test stays green.
    """
    refused = [
        # The exact install this test was written after.
        "RUN python -m pip install --no-cache-dir --extra-index-url "
        'https://download.pytorch.org/whl/cpu "rxnmapper==0.4.3" "rxn-insight==0.1.3"',
        # The same pins with the index flag gone: still a resolution, still no hash.
        'RUN python -m pip install --no-cache-dir "rxnmapper==0.4.3"',
        # A hashed export with an extra index smuggled onto the same line — one `uv.lock` never
        # resolved anything from, spaced and `=`-joined.
        "RUN python -m pip wheel --require-hashes -r /build/requirements.txt "
        "--extra-index-url https://example.invalid/simple",
        "RUN python -m pip wheel --require-hashes -r /build/requirements.txt "
        "--extra-index-url=https://example.invalid/simple",
        # The locked CPU index, but *replacing* PyPI rather than added to it.
        "RUN python -m pip wheel --require-hashes -r /build/requirements.txt "
        "--index-url https://download.pytorch.org/whl/cpu",
        # The locked CPU index beside the locked one, and an unlocked one after it.
        "RUN python -m pip wheel --require-hashes -r /build/requirements.txt "
        "--extra-index-url https://download.pytorch.org/whl/cpu "
        "--extra-index-url https://example.invalid/simple",
        # The locked CPU index on an install that is not the hashed pass.
        "RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels "
        "--extra-index-url https://download.pytorch.org/whl/cpu torch==2.13.0+cpu",
        # Something appended to the bootstrap.
        'RUN python -m pip install --no-cache-dir --upgrade pip "uv>=0.8.17,<1" torch',
        # `--no-deps` over a package name rather than a local path.
        "RUN python -m pip wheel --no-deps --wheel-dir /wheels torch==2.13.0",
    ]
    for instruction in refused:
        assert unhashed_installs([instruction]), f"accepted an unhashed install: {instruction}"

    accepted = [
        f"RUN {_BOOTSTRAP_INSTALL} && uv export --frozen --package x -o /build/requirements.txt "
        "&& python -m pip install --no-cache-dir --require-hashes -r /build/build-requirements.txt "
        "&& python -m pip wheel --no-cache-dir --wheel-dir /wheels "
        "--require-hashes -r /build/requirements.txt "
        "--extra-index-url https://download.pytorch.org/whl/cpu "
        "&& python -m pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir /wheels "
        './packages/mcp_server_kit "./servers/rxnlabel[models]"',
        "RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels "
        'mcp-server-kit "chemclaw-mcp-rxnlabel[models]" && rm -rf /wheels',
    ]
    assert unhashed_installs(accepted) == []


def final_stage_env(instructions: list[str]) -> dict[str, str]:
    """The `ENV` assignments of a Containerfile's last stage — what the running process inherits.

    Only the `KEY=value` form is read, which is the only one this fleet writes; the legacy
    `ENV KEY value` form would come back empty and fail the assertion that reads it, loudly.
    """
    last_from = max(i for i, line in enumerate(instructions) if line.startswith("FROM "))
    env: dict[str, str] = {}
    for line in instructions[last_from:]:
        if line.startswith("ENV "):
            for assignment in line[len("ENV ") :].split():
                name, _, value = assignment.partition("=")
                env[name] = value
    return env


# The three runtimes a torch image sizes itself from, pinned to one thread each in the image as
# `servers/calc/Containerfile` pins its numerical stack. `OMP_NUM_THREADS` is the one torch's
# intra-op pool reads at import; the other two are the BLAS libraries numpy and scipy may bring.
_THREAD_PINS = {"OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}


def _images_carrying_torch() -> list[Path]:
    """The servers whose locked closure — extras included — resolves torch."""
    carrying = []
    for server in server_dirs():
        name = re.search(
            r'^name\s*=\s*"([^"]+)"', (server / "pyproject.toml").read_text(encoding="utf-8"), re.M
        )
        assert name, f"{server.name}/pyproject.toml declares no distribution name"
        if "torch" in _locked_closure(name.group(1)):
            carrying.append(server)
    return carrying


def test_every_torch_image_pins_its_inference_thread_width() -> None:
    """An image that can run torch pins its intra-op width; the node does not choose it."""
    carrying = _images_carrying_torch()
    assert {server.name for server in carrying} >= {"rxnpredict", "rxnlabel"}, (
        "the torch-closure derivation no longer finds the two model servers, so this test would "
        f"pass over none: it found {[server.name for server in carrying]}"
    )
    for server in carrying:
        env = final_stage_env(
            containerfile_instructions((server / "Containerfile").read_text(encoding="utf-8"))
        )
        missing = {k: v for k, v in _THREAD_PINS.items() if env.get(k) != v}
        assert not missing, (
            f"{server.name}/Containerfile's runtime stage carries torch and does not pin "
            f"{sorted(missing)} to 1, so a forward pass's width is the node's core count"
        )


def test_the_env_reader_reads_the_runtime_stage_only() -> None:
    """A pin in a thrown-away build stage reaches no running process, and must not satisfy it."""
    text = "FROM a AS build\nENV OMP_NUM_THREADS=1\nFROM b\nENV MKL_NUM_THREADS=1 \\\n  X=2\n"
    assert final_stage_env(containerfile_instructions(text)) == {"MKL_NUM_THREADS": "1", "X": "2"}


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_every_image_installs_the_closure_the_audit_read(server: Path) -> None:
    """A Containerfile that re-resolves ships something `make deps-audit` never looked at."""
    text = (server / "Containerfile").read_text(encoding="utf-8")
    instructions = containerfile_instructions(text)
    dist = re.search(
        r'^name\s*=\s*"([^"]+)"', (server / "pyproject.toml").read_text(encoding="utf-8"), re.M
    )
    assert dist, f"{server.name}/pyproject.toml declares no distribution name"

    assert any(i == "COPY pyproject.toml uv.lock /build/" for i in instructions), (
        f"{server.name}/Containerfile does not copy uv.lock into its build context, so whatever it "
        "installs was resolved at build time and `make deps-audit` audited a different closure"
    )
    exporting = [
        i for i in instructions if i.startswith("RUN ") and "uv export --frozen --package" in i
    ]
    assert exporting, (
        f"{server.name}/Containerfile runs no `uv export --frozen --package ...`; the third-"
        "party closure is therefore whatever pip resolves on the day of the build"
    )
    assert len(exporting) == 1, (
        f"{server.name}/Containerfile exports the locked closure in {len(exporting)} separate RUN "
        "instructions; this check reads one, so which one ships would be a coin toss"
    )
    block = exporting[0]
    export = re.search(r"uv export --frozen --package ([\w.-]+)", block)
    assert export is not None  # the substring above is what selected this instruction
    assert export.group(1) == dist.group(1), (
        f"{server.name}/Containerfile exports the closure of {export.group(1)!r} while its own "
        f"distribution is {dist.group(1)!r}: it would install another server's dependencies"
    )
    assert "--require-hashes -r /build/requirements.txt" in block, (
        f"{server.name}/Containerfile does not install the exported closure with "
        "`--require-hashes` in the same RUN that exports it, so a rewritten artefact under an "
        "unchanged version installs quietly"
    )


def test_the_image_install_check_reads_instructions_and_not_the_text() -> None:
    """The bite test for the check above: a comment saying it must not satisfy it."""
    comments_only = """FROM python:3.11-slim
# COPY pyproject.toml uv.lock /build/
# RUN uv export --frozen --package chemclaw-mcp-props \\
#       && pip wheel --require-hashes -r /build/requirements.txt
COPY pyproject.toml /build/
RUN python -m pip wheel --wheel-dir /wheels chemclaw-mcp-props
"""
    assert containerfile_instructions(comments_only) == [
        "FROM python:3.11-slim",
        "COPY pyproject.toml /build/",
        "RUN python -m pip wheel --wheel-dir /wheels chemclaw-mcp-props",
    ], "a comment reached the instruction list, which is exactly how the old check was satisfied"

    split_across_runs = """COPY pyproject.toml uv.lock /build/
RUN uv export --frozen --package chemclaw-mcp-props --format requirements-txt \\
      -o /build/requirements.txt
RUN python -m pip wheel --require-hashes -r /build/requirements.txt
"""
    instructions = containerfile_instructions(split_across_runs)
    exporting = [
        i for i in instructions if i.startswith("RUN ") and "uv export --frozen --package" in i
    ]
    assert len(exporting) == 1
    assert "--require-hashes -r /build/requirements.txt" not in exporting[0], (
        "the two halves landed in one instruction, so the same-RUN assertion above proves nothing"
    )

    joined = containerfile_instructions("RUN a \\\n    && b \\\n    && c\n")
    assert joined == ["RUN a && b && c"], "a continuation was not joined into one instruction"


def _lock() -> dict[str, object]:
    import tomllib

    return tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))


def _sdist_only_distributions() -> set[str]:
    """Registry packages `uv.lock` resolves to a source archive and to no wheel at all.

    These are the only entries a `pip wheel --require-hashes` run actually *builds*, and building
    is where pip's default isolation fetches a backend the lock never saw.
    """
    packages = _lock()["package"]
    assert isinstance(packages, list)
    return {
        str(entry["name"])
        for entry in packages
        if "registry" in entry.get("source", {}) and entry.get("sdist") and not entry.get("wheels")
    }


def _locked_closure(distribution: str) -> set[str]:
    """Every distribution reachable from `distribution` through `uv.lock`, extras included.

    Images install extras, and over-approximating the closure can only make a server owe the
    build-backend pin, never excuse it.
    """
    packages = _lock()["package"]
    assert isinstance(packages, list)
    entries: dict[str, list[dict[str, object]]] = {}
    for entry in packages:
        entries.setdefault(str(entry["name"]), []).append(entry)

    seen: set[str] = set()
    stack = [distribution]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        for entry in entries.get(name, []):
            dependencies = entry.get("dependencies", [])
            assert isinstance(dependencies, list)
            requirements: list[dict[str, object]] = list(dependencies)
            optional = entry.get("optional-dependencies", {})
            assert isinstance(optional, dict)
            for extra in optional.values():
                requirements.extend(extra)
            stack.extend(str(requirement["name"]) for requirement in requirements)
    return seen


def _wheel_pass(block: str, distinguishing: str) -> str:
    """The one `pip wheel` invocation inside `block` that carries `distinguishing`.

    The build stage runs two (third-party with `--require-hashes`, workspace with `--no-deps`); a
    substring match over the whole RUN would let a flag on one satisfy an assertion about the other.
    """
    passes = [f"pip wheel {part}" for part in block.split("pip wheel ")[1:]]
    matching = [invocation for invocation in passes if distinguishing in invocation.split("&&")[0]]
    assert len(matching) == 1, (
        f"expected exactly one `pip wheel` carrying {distinguishing!r}, found {len(matching)}"
    )
    return matching[0].split("&&")[0]


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_a_sdist_only_dependency_builds_under_a_pinned_backend(server: Path) -> None:
    """A dependency with no wheel is *built* in the image, under a backend the lock must name."""
    block = next(
        instruction
        for instruction in containerfile_instructions(
            (server / "Containerfile").read_text(encoding="utf-8")
        )
        if instruction.startswith("RUN ") and "uv export --frozen --package" in instruction
    )
    distribution = re.search(
        r'^name\s*=\s*"([^"]+)"', (server / "pyproject.toml").read_text(encoding="utf-8"), re.M
    )
    assert distribution, f"{server.name}/pyproject.toml declares no distribution name"

    built = sorted(_sdist_only_distributions() & _locked_closure(distribution.group(1)))
    if not built:
        pytest.skip(f"{server.name}'s locked closure builds no sdist")

    third_party = _wheel_pass(block, "--require-hashes -r /build/requirements.txt")
    assert "--only-group build" in block and (
        "--require-hashes -r /build/build-requirements.txt" in block
    ), (
        f"{server.name}/Containerfile installs {built!r}, which uv.lock resolves to an sdist "
        "with no wheel, so pip builds it — and with pip's default build isolation the backend "
        "that runs its setup.py is resolved from PyPI at build time, unhashed and outside the "
        "lock. Export `--only-group build` and install it with `--require-hashes` in this same RUN"
    )
    assert "--no-build-isolation" in third_party, (
        f"{server.name}/Containerfile pins a build backend and then does not use it on the pass "
        f"that builds {built!r}: without `--no-build-isolation` pip still fetches its own, and the "
        "pinned one is dead weight"
    )


@pytest.mark.parametrize("server", server_dirs(), ids=lambda path: path.name)
def test_every_image_builds_its_workspace_wheels_under_the_locked_backend(server: Path) -> None:
    """The second `pip wheel` pass builds this fleet's own wheels under the lock's backend."""
    block = next(
        instruction
        for instruction in containerfile_instructions(
            (server / "Containerfile").read_text(encoding="utf-8")
        )
        if instruction.startswith("RUN ") and "uv export --frozen --package" in instruction
    )
    workspace = _wheel_pass(block, "--no-deps")

    assert "--only-group build" in block, (
        f"{server.name}/Containerfile never exports the `build` group, so the backend its "
        "workspace wheels are built with is whatever pip resolves from PyPI that day"
    )
    assert "--require-hashes -r /build/build-requirements.txt" in block, (
        f"{server.name}/Containerfile exports the `build` group and installs it without "
        "`--require-hashes`, which is the export's whole point"
    )
    assert "--no-build-isolation" in workspace, (
        f"{server.name}/Containerfile installs a pinned backend and then builds its own two "
        "distributions in isolation anyway: pip fetches its own hatchling and the pinned one is "
        "dead weight"
    )


def test_the_build_group_names_every_backend_this_workspace_declares() -> None:
    """Every `build-system.requires` in this workspace is in the `build` group and lock."""
    import tomllib

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    group = declared["dependency-groups"]["build"]
    named = {re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0].strip() for requirement in group}

    manifests = [ROOT / "pyproject.toml"]
    manifests += sorted(ROOT.glob("packages/*/pyproject.toml"))
    manifests += sorted(ROOT.glob("servers/*/pyproject.toml"))
    backends: dict[str, set[str]] = {}
    for manifest in manifests:
        requires = tomllib.loads(manifest.read_text(encoding="utf-8")).get("build-system", {})
        for requirement in requires.get("requires", []):
            backend = re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0].strip()
            backends.setdefault(backend, set()).add(str(manifest.relative_to(ROOT)))

    missing = {name: sorted(where) for name, where in backends.items() if name not in named}
    assert not missing, (
        f"the `build` dependency group does not name {sorted(missing)}, declared as a build "
        f"backend by {missing}. Those images build `--no-build-isolation`, so the backend has "
        "to come from that group or it comes from nowhere"
    )

    locked = _lock()["package"]
    assert isinstance(locked, list)
    resolved = {str(entry["name"]) for entry in locked}
    unlocked = sorted(name for name in named if name not in resolved)
    assert not unlocked, (
        f"`uv.lock` resolves no {unlocked!r}, so `uv export --only-group build` omits it and the "
        "`--require-hashes` install that is supposed to pin the backend installs nothing"
    )


def test_the_build_group_is_what_the_calc_image_exports() -> None:
    """The group the calc Containerfile exports exists in the lock and holds a build backend.

    Exporting a missing group yields an empty requirements file, the install succeeds, and
    `--no-build-isolation` then builds against whatever the base image ships.
    """
    import tomllib

    groups = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "dependency-groups"
    ]
    assert "build" in groups, (
        "servers/calc/Containerfile exports `--only-group build`; the root pyproject.toml declares "
        "no such group, so the export is empty and the backend pin is a no-op"
    )
    assert any(requirement.startswith("setuptools") for requirement in groups["build"]), (
        "the `build` group names no setuptools; geometric's sdist is a legacy setup.py and builds "
        "under nothing else"
    )
    locked = _lock()["package"]
    assert isinstance(locked, list)
    assert any(entry["name"] == "setuptools" for entry in locked), (
        "uv.lock resolves no setuptools, so `uv export --only-group build` cannot pin one"
    )
