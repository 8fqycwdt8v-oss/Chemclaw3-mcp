"""The fleet's calculation key: the hash, the envelope, and the off-pod `calc_version` rule.

The literal digests are Chemclaw3's own output (`servers/calc/tests/test_key_contract.py` has how
they were taken), so a change here is a change to every cache row on the consumer's side.
"""

from __future__ import annotations

import pytest
from mcp_server_kit.keys import (
    NOT_A_COMPUTE_TOOL,
    CalculationKey,
    not_a_compute_tool,
    off_pod_calc_version,
    stable_hash,
)

DIGEST = "sha256:" + "ab" * 32


def test_the_hash_is_the_one_chemclaw3_computes() -> None:
    """Sorted keys, tight separators, SHA-256, sixteen hex characters."""
    assert stable_hash({"b": 2, "a": [1, "x"]}) == "8cbd548a32262b76"
    assert stable_hash({"smiles": "CCO"}) == "a7d334ebee616d78"


def test_build_hashes_inputs_bare_and_params_inside_the_epoch_envelope() -> None:
    """The two `stable_hash` calls, pinned against Chemclaw3's output for epoch 2."""
    key = CalculationKey.build(
        "solubility", "esol-delaney@2004/rdkit-2026.3.5/u-0.75", {"smiles": "CCO"}, epoch="2"
    )
    assert key.as_str() == (
        "solubility@esol-delaney@2004/rdkit-2026.3.5/u-0.75:a7d334ebee616d78:3ba6ef80c850abd1"
    )
    assert key.params_hash == stable_hash({"epoch": "2", "params": None})


def test_build_refuses_without_an_epoch() -> None:
    """The epoch is the server's; a key without one would collide across every server's bump."""
    with pytest.raises(ValueError, match="epoch"):
        CalculationKey.build("t", "v", {"x": 1})


def test_a_different_epoch_moves_only_params_hash() -> None:
    """A server's bump invalidates its rows and leaves the ledger key (`calc_version`) alone."""
    one = CalculationKey.build("t", "v", {"x": 1}, {"p": 2}, epoch="1")
    two = CalculationKey.build("t", "v", {"x": 1}, {"p": 2}, epoch="2")
    assert one.params_hash != two.params_hash
    assert (one.calc_type, one.calc_version, one.input_hash) == (
        two.calc_type,
        two.calc_version,
        two.input_hash,
    )


def test_an_off_pod_version_names_method_engine_build_and_image() -> None:
    """Four parts, `+`-separated, the method first so it alone may contain `+`."""
    version = off_pod_calc_version("B3LYP/6-311+G**", "orca", "6.0.1", DIGEST)
    assert version == f"B3LYP/6-311+G**+orca+6.0.1+{DIGEST}"
    assert version.rsplit("+", 3) == ["B3LYP/6-311+G**", "orca", "6.0.1", DIGEST]


def test_two_builds_of_one_engine_never_share_a_version() -> None:
    """The reason the image digest is in it: two clusters, one engine version, two builds."""
    other = "sha256:" + "cd" * 32
    assert off_pod_calc_version("m", "orca", "6.0.1", DIGEST) != off_pod_calc_version(
        "m", "orca", "6.0.1", other
    )


@pytest.mark.parametrize(
    ("method", "engine", "engine_version", "digest", "message"),
    [
        ("", "orca", "6.0.1", DIGEST, "method"),
        ("m", "", "6.0.1", DIGEST, "engine"),
        ("m", "or+ca", "6.0.1", DIGEST, "engine"),
        ("m", "orca", " 6.0.1", DIGEST, "engine_version"),
        ("m", "orca", "6.0.1", "latest", "image_digest"),
        ("m", "orca", "6.0.1", "sha256:" + "AB" * 32, "image_digest"),
    ],
)
def test_an_ambiguous_or_movable_part_is_refused(
    method: str, engine: str, engine_version: str, digest: str, message: str
) -> None:
    """A tag can move under a key and a `+` in a later part makes the string ambiguous."""
    with pytest.raises(ValueError, match=message):
        off_pod_calc_version(method, engine, engine_version, digest)


def test_the_non_compute_refusal_carries_the_phrase_the_fleet_test_reads() -> None:
    """`tests/test_fleet_compute_keys.py` tells "not a calculation" apart by this phrase."""
    refusal = not_a_compute_tool("embed_structure", ["b_tool", "a_tool"])
    assert NOT_A_COMPUTE_TOOL in str(refusal)
    assert str(refusal).endswith("a_tool, b_tool")
