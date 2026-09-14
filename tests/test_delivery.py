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


def test_a_publishing_run_cannot_skip_the_gate() -> None:
    """`RUN_GATE` defaults off, and nothing in this pipeline can see GitHub Actions.

    The parameter's description said "off because GitHub Actions is the gate", which is a true
    sentence about a system this file never consults: no step reads a check run, a status or a
    conclusion for `env.REVISION`. So every publishing run could ship an image built from a
    revision whose `make check` had never run
    (`D-2026-09-13-a-gate-in-another-system-is-not-a-gate-this-one-can-see`).

    Read as source rather than driven, because driving it needs a Jenkins. Three separate facts,
    because the refusal is wrong if any one of them is missing and each fails differently:

    - the refusal names **all three** conditions - a publish is `!DRY_RUN` *and* a registry *and*
      `!RUN_GATE`, and dropping the registry term would refuse a build-only run that ships nothing;
    - it `error`s rather than warns, because a pipeline that logs and continues has published by
      the time anybody reads the log;
    - the `Gate` stage it points at still runs `make check`, or the refusal sends an operator to a
      stage that proves nothing.
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
    """Two spellings of one predicate disagreed, and Groovy's truthiness is where they disagreed.

    The Preflight refusal asked `params.IMAGE_REGISTRY?.trim()` and the publish branch asked
    `!params.IMAGE_REGISTRY`. A whitespace-only string is **truthy** in Groovy while its `trim()` is
    not, so `IMAGE_REGISTRY="  "` with `DRY_RUN=false` and `RUN_GATE=false` skipped the refusal and
    took the publishing branch. The push then died on an image reference of `"  /chemclaw-mcp-…"`,
    which makes it an inconsistency rather than a live bypass — and an inconsistency between two
    spellings of one question is the thing to delete, not the accident that saves it. A third
    spelling, `params.IMAGE_REGISTRY != ''`, gated the digest report.

    So the pipeline trims once into `env.IMAGE_REGISTRY` and every later read is of that. This is
    read as source because driving it needs a Jenkins; what it asserts is the property that made the
    three spellings possible — that `params.IMAGE_REGISTRY` is read exactly once, where it is
    normalised.
    """
    # The comment block above the assignment explains the defect and quotes both old spellings, so
    # **every** assertion below reads the comment-stripped code. Driven while writing this: with the
    # normalisation commented out and `env.IMAGE_REGISTRY = params.IMAGE_REGISTRY` in its place,
    # a version of this test that matched the raw text passed
    # (`D-2026-09-14-a-ratchet-that-matches-a-comment-holds-nothing`, in the file that records it).
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
