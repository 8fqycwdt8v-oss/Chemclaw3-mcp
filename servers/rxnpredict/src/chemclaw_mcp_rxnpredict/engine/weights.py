"""The baked model weights, checked against the digests the build recorded for them.

Every other corpus in this fleet is verified on load — `props`' solvent table, this server's own
trust priors — and the artifact that decides what this server actually predicts was the one
exception. The build wrote a `SHA256SUMS` and nothing ever read it, which is the same shape as a
README asserting a control: an artifact that looks like a check and is not one.

**What this is, and is not.** It is an *integrity* check on the copy: the weights the runtime image
holds are the ones the builder stage fetched, so a truncated layer, a bad `COPY` or a swapped
read-only volume is caught. It is not a *provenance* check — the digests are generated in the same
build that fetches the models, so they cannot testify to which model was approved. That job belongs
to the pinned revisions in `scripts/fetch_models.py`, which are in git and are reviewed in a pull
request like any other dependency.

**Why the manifest is allowed to be absent.** A checkout with no baked weights is the normal state
of this repository, of the test suite, and of a deployment running the deterministic doubles. A
verifier that treated "no weights" as a failure would fail everywhere it is not needed, so an absent
manifest is reported as nothing-to-verify and a present one is checked in full.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DIGEST_FILE = "SHA256SUMS"


@dataclass(frozen=True, slots=True)
class WeightsReport:
    """What verification found, in a form a caller can log or assert on."""

    manifest: Path | None
    checked: int = 0
    mismatched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        """Whether every file the manifest names is present and matches."""
        return not self.mismatched and not self.missing

    def summary(self) -> str:
        """One line for the startup log, saying plainly which of the three cases this is."""
        if self.manifest is None:
            return "no model digest manifest found; nothing to verify"
        if self.verified:
            return f"verified {self.checked} model file(s) against {self.manifest}"
        return (
            f"model weights do not match {self.manifest}: "
            f"{len(self.mismatched)} changed, {len(self.missing)} missing"
        )


def _digest(path: Path) -> str:
    """The SHA-256 of `path`, read in blocks so a multi-gigabyte checkpoint stays out of memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse(text: str) -> list[tuple[str, str]]:
    """Parse `sha256sum` output into `(digest, relative path)` pairs.

    The coreutils format is `<hex><two spaces or space-star><path>`. The star marks binary mode,
    which is what a checkpoint is read in, so both spellings have to be accepted.
    """
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        digest, _, name = stripped.partition(" ")
        name = name.lstrip(" *")
        if digest and name:
            entries.append((digest.lower(), name))
    return entries


def verify_weights(model_dir: Path) -> WeightsReport:
    """Check every file `model_dir/SHA256SUMS` names against its recorded digest.

    Args:
        model_dir: The directory the image bakes the weights into — `Settings.model_dir`.

    Returns:
        A `WeightsReport`. Never raises for a missing manifest or a missing file: the caller decides
        what a failure is worth, because a build step wants a non-zero exit and a serving process
        wants a loud log rather than a refusal to start.
    """
    manifest = model_dir / DIGEST_FILE
    if not manifest.is_file():
        return WeightsReport(manifest=None)

    mismatched: list[str] = []
    missing: list[str] = []
    checked = 0
    for expected, name in _parse(manifest.read_text(encoding="utf-8")):
        target = (model_dir / name).resolve()
        if not target.is_file():
            missing.append(name)
            continue
        checked += 1
        if _digest(target) != expected:
            mismatched.append(name)
    return WeightsReport(
        manifest=manifest,
        checked=checked,
        mismatched=tuple(sorted(mismatched)),
        missing=tuple(sorted(missing)),
    )
