"""Runtime configuration for `rxnpredict`.

Forked from `chemclaw2_forward.config`, with three changes that follow from this fleet's rules:

- **Every variable is prefixed `CHEMCLAW_RXNPREDICT_`.** Upstream read bare names
  (`ENABLED_FORWARD_MODELS`, `DEVICE`), which is fine for a repository that owns its process and a
  liability the moment several servers share a deployment's environment.
- **The Anthropic settings are gone**, with the predictor that used them. No server here reaches a
  third-party API, so there is nothing for a key to configure.
- **The model directory is read, never created.** Upstream did `mkdir(parents=True)` on first use;
  the image built here bakes the weights in at build time and runs rootless with a read-only root
  filesystem, so creating that directory would fail — and would mean the weights were missing.

Per-class trust priors are no longer read from a home directory. They are a **vendored dataset**
with a licence and a checksum (`data/trust_priors.json`), because they are numbers that change every
ranking this server produces and a number with no recorded provenance is one nobody can defend a
year later.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mcp_server_kit.limits import echo, report_settings
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

#: Per-model trust priors, seeded from the benchmarks each predictor's own paper reports. A
#: read-only view so the one table every `Settings` starts from cannot be edited in place.
DEFAULT_MODEL_TRUST_PRIORS: Mapping[str, float] = MappingProxyType(
    {
        # Forward
        "reaction_t5_v2": 1.00,  # ~97.5% top-1 USPTO-MIT (Sagawa 2024)
        "molecular_transformer": 0.90,  # ~90% top-1
        "t5chem": 0.92,
        "chemformer": 0.85,
        "megan": 0.80,
        "graphrxn": 0.80,
        # Conditions
        "parrot": 0.95,  # +13.44% top-3 over the Coley baseline
        "rxn_insight": 0.70,  # rule-based: fast, coarse
        "two_stage_dnn": 0.90,  # 73% top-10 exact match
        "reagents_mt": 0.80,
        "askcos_condition": 0.85,
    }
)


def _predictor_weights(supplied: object, field: str) -> dict[str, float]:
    """`supplied` as `{predictor id: weight}`, or a `ValueError` naming what it cannot mean.

    Shared by the global table and each class of the per-class one, so the two cannot disagree
    about what a weight is: a predictor `DEFAULT_MODEL_TRUST_PRIORS` weights — a typo would set
    nothing and read as done — and a finite number above zero, since zero silences a predictor
    that still reports having voted and a negative one inverts its vote.
    """
    if not isinstance(supplied, dict):
        raise ValueError(
            f"{field} must be a JSON object of predictor id to weight, "
            f"not {type(supplied).__name__}"
        )
    unknown = sorted(str(name) for name in set(supplied) - set(DEFAULT_MODEL_TRUST_PRIORS))
    if unknown:
        raise ValueError(
            f"{field} names {echo(repr(unknown))}, which this server does not "
            f"weight; the predictors it does are {sorted(DEFAULT_MODEL_TRUST_PRIORS)}"
        )
    weights: dict[str, float] = {}
    for name, weight in supplied.items():
        if isinstance(weight, bool) or not isinstance(weight, int | float):
            raise ValueError(f"{field}[{echo(name)!r}] must be a number, not {echo(repr(weight))}")
        if not (math.isfinite(weight) and weight > 0):
            raise ValueError(
                f"{field}[{echo(name)!r}] must be finite and above zero, not {weight!r}; "
                "to stop a predictor voting, name it in CHEMCLAW_RXNPREDICT_DISABLED_MODELS"
            )
        weights[str(name)] = float(weight)
    return weights


class Settings(BaseSettings):
    """Server configuration, from `CHEMCLAW_RXNPREDICT_*` environment variables.

    Predictor selection is comma-separated, and `*` means "every predictor that registered":

        CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=reaction_t5_v2
        CHEMCLAW_RXNPREDICT_DISABLED_MODELS=megan
    """

    model_config = SettingsConfigDict(
        env_prefix="CHEMCLAW_RXNPREDICT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Model selection ---
    enabled_forward_models: str = Field(default="*", description="Comma list or '*' for all.")
    enabled_conditions_models: str = Field(default="*", description="Comma list or '*' for all.")
    disabled_models: str = Field(
        default="", description="Comma list of predictor IDs to force off."
    )

    # --- Baked model weights ---
    # Read-only, and pointed at the path the Containerfile bakes into the image. A deployment can
    # move it (a mounted read-only volume of weights is the other sane arrangement); nothing here
    # creates or writes it.
    model_dir: Path = Field(default=DATA_DIR / "models")

    # --- Compute ---
    device: str = Field(default="auto", description="'cpu', 'cuda', 'cuda:0', or 'auto'.")
    default_top_k: int = Field(default=5, ge=1, le=50)

    # --- Prediction cache (in-process, bounded; see `cache.py`) ---
    cache_enabled: bool = Field(default=True)
    cache_max_entries: int = Field(default=2048, ge=0)

    # --- Mixture-of-experts gating ---
    use_class_priors: bool = Field(
        default=True,
        description="Use per-reaction-class trust priors in the aggregator when available.",
    )

    # Per-model trust priors used by the aggregator. Higher = more weight in voting. The table is
    # `DEFAULT_MODEL_TRUST_PRIORS`; a JSON env value **adjusts named entries of it** rather than
    # replacing it — see `_merge_priors`.
    model_trust_priors: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_MODEL_TRUST_PRIORS)
    )

    # Per-reaction-class priors, as an explicit JSON env **adjustment** and nothing else. Empty is
    # the shipped state and means "the vendored table as calibrated". A value names
    # `{class: {predictor: weight}}` pairs, and `class_priors()` lays them over the corpus — lazily,
    # because the corpus is read on the probe and never at settings load.
    # **Never read this field directly on a serving path**; read `class_priors()`, which is the
    # only place that knows how the two combine.
    model_trust_priors_by_class: dict[str, dict[str, float]] = Field(default_factory=dict)

    @field_validator("model_trust_priors", mode="before")
    @classmethod
    def _merge_priors(cls, value: Any) -> Any:
        """Overlay a supplied table onto `DEFAULT_MODEL_TRUST_PRIORS`, refusing what it cannot mean.

        **An environment value used to replace the whole table.** Measured on 2026-09-12,
        `CHEMCLAW_RXNPREDICT_MODEL_TRUST_PRIORS='{"parrot": 9.9}'` left the aggregator with one
        prior, so every other predictor fell to `effective_prior`'s unweighted default of 0.5 —
        `reaction_t5_v2` halved from 1.00, with no error and nothing in the answer to say so. The
        operator asked to change one weight and changed eleven. So a supplied table is an
        *adjustment*: the entries it names replace those defaults and the rest stand. A full
        replacement is still expressible by naming every predictor.

        Two more things a JSON string can carry and the aggregator cannot mean are refused rather
        than absorbed: a key that is not a predictor this table weights — a typo would otherwise
        set nothing and read as done — and a weight that is not finite and positive, since zero
        silences a predictor that still reports having voted (`DISABLED_MODELS` is the switch for
        that) and a negative one inverts its vote.

        Accepts a JSON string as well as a mapping, which is how an env var can carry one.
        """
        supplied = json.loads(value) if isinstance(value, str) else value
        merged = dict(DEFAULT_MODEL_TRUST_PRIORS)
        merged.update(_predictor_weights(supplied, "model_trust_priors"))
        return merged

    @field_validator("model_trust_priors_by_class", mode="before")
    @classmethod
    def _parse_class_priors(cls, value: Any) -> Any:
        """Validate a per-class adjustment; `class_priors()` is what lays it over the corpus.

        **It used to be the whole per-class table**, with nothing checked: one class named in
        `CHEMCLAW_RXNPREDICT_MODEL_TRUST_PRIORS_BY_CLASS` dropped every other class's calibrated
        weights from `data/trust_priors.json`, a misspelt class label or predictor id set nothing
        and read as done, and a zero or negative weight silenced or inverted a vote — the defect
        `_merge_priors` fixed for the global table, one level down
        (`D-2026-09-26-a-class-prior-adjusts-the-corpus-it-does-not-replace-it`).

        So the same rules apply per class: an object of class label to an object of predictor id to
        weight; every label one `classifier.ALL_CLASSES` names **other than `CLASS_OTHER`**, which
        `effective_prior` never selects a per-class weight for, so a prior on it would change
        nothing and read as done; every predictor one `DEFAULT_MODEL_TRUST_PRIORS` weights; every
        weight finite and above zero. The corpus is not read here: that would put a checksum
        failure back into settings load, which is an import-time crash
        (`D-2026-09-18-a-corpus-that-cannot-be-read-is-a-probe-s-answer-not-an-import-error`).
        """
        from chemclaw_mcp_rxnpredict.engine.meta.classifier import ALL_CLASSES, CLASS_OTHER

        supplied = json.loads(value) if isinstance(value, str) else value
        if not isinstance(supplied, dict):
            raise ValueError(
                "model_trust_priors_by_class must be a JSON object of reaction class to "
                f"{{predictor id: weight}}, not {type(supplied).__name__}"
            )
        selectable = ALL_CLASSES - {CLASS_OTHER}
        unknown = sorted(str(label) for label in supplied if label not in selectable)
        if unknown:
            raise ValueError(
                f"model_trust_priors_by_class names {echo(repr(unknown))}, which is not a class "
                f"a per-class prior is ever read for; the classes are {sorted(selectable)}"
            )
        return {
            label: _predictor_weights(weights, f"model_trust_priors_by_class[{label!r}]")
            for label, weights in supplied.items()
        }

    def parse_enabled(self, raw: str) -> set[str] | None:
        """`None` for `*` or empty (meaning "all"), otherwise the named predictor IDs."""
        stripped = raw.strip()
        if stripped in {"*", ""}:
            return None
        return {part.strip() for part in stripped.split(",") if part.strip()}

    def class_priors(self) -> dict[str, dict[str, float]]:
        """The per-reaction-class trust priors this process ranks by: the override, or the corpus.

        **The corpus is read here rather than in `get_settings`, and that is the whole of
        `D-2026-09-18-a-corpus-that-cannot-be-read-is-a-probe-s-answer-not-an-import-error`.**
        `get_settings()` used to populate `model_trust_priors_by_class` eagerly, which made
        *constructing the process settings* depend on `trust_priors.json` passing its checksum —
        and `tools.py` calls `register_requested()` at module scope, which calls `get_settings()`
        for two environment strings it could have had for nothing. Driven on this commit: a
        `trust_priors.json` with one byte appended made `import chemclaw_mcp_rxnpredict.tools`
        raise `DatasetError`, so the pod never started and the reason reached a kubelet as
        `CrashLoopBackOff` instead of as the 503 `/healthz` exists to give.

        Lazy here means the failure lands where `chem` and `safety` already put it: on the probe,
        which runs this (`app.py`'s `_readiness`) before the pod takes traffic.

        **An override adjusts the corpus rather than replacing it**
        (`D-2026-09-26-a-class-prior-adjusts-the-corpus-it-does-not-replace-it`): each
        `(class, predictor)` pair it names replaces that one calibrated weight, and every pair it
        does not name — including every class it does not mention — stands as calibrated. A new
        dict every call, so the cached corpus is never edited in place.

        Returns:
            `{reaction_class: {model_name: weight}}`, empty when no calibration has been run and no
            override is set — which is the shipped state, and makes the aggregator fall back to the
            global priors.
        """
        from chemclaw_mcp_rxnpredict.engine.meta.trust_priors import load_vendored_priors

        corpus = load_vendored_priors(DATA_DIR)
        if not self.model_trust_priors_by_class:
            return corpus
        merged = {label: dict(weights) for label, weights in corpus.items()}
        for label, weights in self.model_trust_priors_by_class.items():
            merged.setdefault(label, {}).update(weights)
        return merged

    def parse_disabled(self) -> set[str]:
        """The predictor IDs forced off, whatever the enabled list says."""
        return {part.strip() for part in self.disabled_models.split(",") if part.strip()}

    def resolve_device(self) -> str:
        """`device`, with `auto` resolved to CUDA when torch can see a GPU."""
        if self.device != "auto":
            return self.device
        try:
            import torch
        except ImportError:
            return "cpu"
        return "cuda" if torch.cuda.is_available() else "cpu"


def inference_threads() -> int:
    """Cores one predictor's forward pass may spend, which is what the admission gate charges it.

    **Read from torch rather than assumed.** `torch.get_num_threads()` is the intra-op width, and
    torch sizes it from the machine's physical cores — *not* from the container's cgroup. The image
    pins `OMP_NUM_THREADS=1`
    (`D-2026-09-26-a-torch-image-pins-one-thread-per-forward-pass`), so this reads 1 there; it is
    still read rather than assumed, because a deployment that raises the pin — or runs this
    package somewhere without it — spends what torch was configured with, and charging the
    ceiling anything else would be charging it for CPU that is not being counted.

    `1` when torch is not importable, which is every checkout without the model extras and is also
    the floor a cost must never fall below: a cost of zero would make the tool uncounted. On CUDA
    the number is still the right charge for this ceiling, because the ceiling bounds this pod's
    *CPU* and the host threads feeding a GPU are what it can see.
    """
    try:
        import torch
    except ImportError:
        return 1
    return max(1, int(torch.get_num_threads()))


_settings: Settings | None = None


def get_settings() -> Settings:
    """The process-wide settings: the environment, parsed once, and nothing read off disk.

    **Nothing here touches a vendored corpus, deliberately.** This function is called at *import*
    of `tools.py` — `register_requested()` needs two environment strings to decide whether the
    configuration named a deterministic double — and it used to load and checksum
    `trust_priors.json` on the way past. The cost of that was measured on this commit: one byte
    appended to that file turned `import chemclaw_mcp_rxnpredict.tools` into a `DatasetError`, so
    the pod crash-looped instead of answering 503 from `/healthz` with the file and both hashes in
    the body. `Settings.class_priors()` is where the corpus is read now, and `app.py`'s readiness
    check is what runs it before the pod takes traffic
    (`D-2026-09-18-a-corpus-that-cannot-be-read-is-a-probe-s-answer-not-an-import-error`).
    """
    global _settings
    if _settings is None:
        _settings = Settings()
        # Its numbers, for `/healthz` (`D-2026-09-26-a-pod-reports-the-bounds-it-is-running-with`).
        report_settings(_settings)
    return _settings


def reset_settings_for_tests() -> None:
    """Force a fresh `Settings`, and a fresh corpus read, on the next `get_settings()`.

    Both halves: the priors are cached beside the settings rather than on them, so resetting only
    the settings object would leave a test's corpus double in place for the next one.
    """
    global _settings
    _settings = None
    from chemclaw_mcp_rxnpredict.engine.meta.trust_priors import (
        load_vendored_priors,
        priors_dataset,
    )

    load_vendored_priors.cache_clear()
    priors_dataset.cache_clear()
