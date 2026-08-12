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
import logging
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


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

    # Per-model trust priors used by the aggregator. Higher = more weight in voting. Seeded from
    # the benchmarks each predictor's own paper reports; overridable as a JSON env value.
    model_trust_priors: dict[str, float] = Field(
        default_factory=lambda: {
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

    # Per-reaction-class priors, loaded from the vendored dataset by `get_settings`. An explicit
    # JSON env value wins, so an operator can override without rebuilding the image.
    model_trust_priors_by_class: dict[str, dict[str, float]] = Field(default_factory=dict)

    @field_validator("model_trust_priors", mode="before")
    @classmethod
    def _parse_priors(cls, value: Any) -> Any:
        """Accept the priors as a JSON string, which is how an env var can carry a mapping."""
        return json.loads(value) if isinstance(value, str) else value

    @field_validator("model_trust_priors_by_class", mode="before")
    @classmethod
    def _parse_class_priors(cls, value: Any) -> Any:
        """Same, for the per-class table."""
        return json.loads(value) if isinstance(value, str) else value

    def parse_enabled(self, raw: str) -> set[str] | None:
        """`None` for `*` or empty (meaning "all"), otherwise the named predictor IDs."""
        stripped = raw.strip()
        if stripped in {"*", ""}:
            return None
        return {part.strip() for part in stripped.split(",") if part.strip()}

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


_settings: Settings | None = None


def get_settings() -> Settings:
    """The process-wide settings, with the vendored per-class priors loaded once.

    The priors are read through `load_dataset`, so a truncated or swapped file fails here — at
    startup, with both checksums in the message — rather than silently changing how every
    prediction in this server is ranked.
    """
    global _settings
    if _settings is None:
        settings = Settings()
        if not settings.model_trust_priors_by_class:
            from chemclaw_mcp_rxnpredict.engine.meta.trust_priors import load_vendored_priors

            settings.model_trust_priors_by_class = load_vendored_priors(DATA_DIR)
            if settings.model_trust_priors_by_class:
                logger.info(
                    "loaded per-class trust priors for %d reaction classes",
                    len(settings.model_trust_priors_by_class),
                )
        _settings = settings
    return _settings


def reset_settings_for_tests() -> None:
    """Force a fresh `Settings` on the next `get_settings()`."""
    global _settings
    _settings = None
