"""The delivery pipeline describes this fleet; these are the halves a file can check.

`Jenkinsfile` cannot run here — there is no controller, no registry, no cluster. What can be checked
is every claim it makes about *this tree*, and one property that matters more than the rest:

**it must derive the server list from the filesystem rather than carry one.**

That is not a style preference. Chemclaw3's image workflow kept a hand-written component list and
went on smoking `workers.hpc_worker` for months after the component ceased to exist — a green gate
asserting something absent. This repository adds servers regularly (seven now, five more `proposed`
in `MODULES.md`), so a list written into a pipeline is a list that is wrong by the next merge, and
wrong in the direction that fails open: a server nobody builds is a server nobody deploys, silently.

Deliberately not checked: whether any of it works against a registry. Nothing here can know that.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JENKINSFILE = ROOT / "Jenkinsfile"
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
    """The two facts only a started container can establish, and both have failed elsewhere.

    The revision reaching `/healthz` is what `test_the_revision_reaches_the_handshake_and_the_probe`
    asserts of the *file*; Chemclaw3's own revision field read `unknown` in every build for eight
    months with its test green, because nothing ever set the build argument.

    Bearer enforcement on `/mcp` cannot be read off the source at all: the MCP surface is *mounted*,
    and a mount bypasses the enclosing app's dependencies. `CLAUDE.md` says to verify it against a
    running server for exactly this reason.
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


def _shell_as_the_shell_receives_it(block: str) -> str:
    r"""Resolve a Groovy GString to the text bash is actually handed.

    `${...}` is interpolated by Jenkins before the shell sees anything; `\${...}` and `\$(...)`
    reach the shell verbatim — that escape is how a pipeline writes a *shell* variable inside an
    interpolated string, and getting it backwards is the most common way one of these files breaks.
    """
    resolved = re.sub(r"(?<!\\)\$\{[^}]*\}", "PLACEHOLDER", block)
    return resolved.replace("\\$", "$").replace("\\\\", "\\")


def test_every_shell_block_in_the_pipeline_parses() -> None:
    """The one thing about this pipeline that can actually be executed here.

    A Jenkinsfile is checked by no compiler and no linter in this repository, and its shell bodies
    are strings — so an unbalanced quote is invisible until a run, against a registry. `bash -n`
    costs milliseconds, and speaks about the text the shell receives rather than the text in
    the file.
    """
    text = _pipeline()
    blocks = re.findall(r'"""(.*?)"""', text, re.S) + re.findall(r"sh '''(.*?)'''", text, re.S)
    assert len(blocks) >= 3, f"only {len(blocks)} shell blocks found — the parse has drifted"
    for block in blocks:
        script = _shell_as_the_shell_receives_it(block)
        result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
        assert result.returncode == 0, f"a shell block does not parse: {result.stderr.strip()}"
