"""Pydantic schemas shared across predictors, aggregator, and HTTP/MCP layers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ForwardPrediction(BaseModel):
    """One predicted product from a single forward-prediction model."""

    model_config = ConfigDict(frozen=True)

    product_smiles: str = Field(..., description="Canonical SMILES of the predicted product.")
    score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Model-native confidence normalised to [0, 1]. Higher is better.",
    )
    rank: int = Field(..., ge=1, description="1-based rank within this model's top-K.")
    source_model: str = Field(..., description="Predictor identifier (e.g. 'reaction_t5_v2').")


class ConditionsPrediction(BaseModel):
    """One predicted condition set from a single condition-prediction model."""

    model_config = ConfigDict(frozen=True)

    catalysts: list[str] = Field(default_factory=list)
    solvents: list[str] = Field(default_factory=list)
    reagents: list[str] = Field(default_factory=list)
    temperature_c: float | None = Field(default=None, description="Temperature in degrees Celsius.")
    score: float = Field(..., ge=0.0, le=1.0)
    rank: int = Field(..., ge=1)
    source_model: str


class AggregatedForwardPrediction(BaseModel):
    """A meta-model consensus product with per-source attribution."""

    product_smiles: str
    consensus_score: float = Field(..., ge=0.0, le=1.0)
    rank: int = Field(..., ge=1)
    vote_count: int = Field(
        ..., ge=1, description="Number of predictors that voted for this product."
    )
    contributing_models: list[str]


class AggregatedConditionsPrediction(BaseModel):
    """A meta-model consensus condition set."""

    catalysts: list[str]
    solvents: list[str]
    reagents: list[str]
    temperature_c: float | None
    consensus_score: float = Field(..., ge=0.0, le=1.0)
    rank: int = Field(..., ge=1)
    vote_count: int = Field(..., ge=1)
    contributing_models: list[str]


# ---------- Request / response envelopes ----------


class ForwardRequest(BaseModel):
    reactants: str = Field(
        ...,
        description="Dot-separated SMILES of reactants (and optional reagents joined by '>'), "
        "e.g. 'CC(=O)Cl.Nc1ccccc1' or 'CC(=O)Cl.Nc1ccccc1>>'.",
    )
    top_k: int = Field(default=5, ge=1, le=50, description="Top-K predictions per model.")
    models: list[str] | None = Field(
        default=None,
        description="Subset of predictor IDs to query. None = all enabled.",
    )


class ConditionsRequest(BaseModel):
    reactants: str = Field(..., description="Dot-separated SMILES of reactants.")
    product: str = Field(..., description="SMILES of the target product.")
    top_k: int = Field(default=5, ge=1, le=50)
    models: list[str] | None = None


class ForwardResponse(BaseModel):
    consensus: list[AggregatedForwardPrediction]
    per_model: dict[str, list[ForwardPrediction]]
    canonical_reactants: str
    n_models_queried: int
    n_models_succeeded: int
    source: str = Field(
        default="",
        description="Which predictors voted, and under which trust priors. Quote it.",
    )


class ConditionsResponse(BaseModel):
    consensus: list[AggregatedConditionsPrediction]
    per_model: dict[str, list[ConditionsPrediction]]
    canonical_reactants: str
    canonical_product: str
    n_models_queried: int
    n_models_succeeded: int
    source: str = Field(
        default="",
        description="Which predictors voted, and under which trust priors. Quote it.",
    )


class ModelInfo(BaseModel):
    name: str
    kind: Literal["forward", "conditions"]
    available: bool = Field(
        ..., description="Whether this predictor's code and dependencies loaded in this build."
    )
    enabled: bool = Field(
        default=True,
        description="Whether this deployment will actually run it. A loaded predictor an operator "
        "turned off is available and not enabled, and no tool here will call it.",
    )
    description: str
    citation: str | None = None
    extras_install: str | None = Field(
        default=None,
        description="pip extra to install this predictor's dependencies, e.g. 'reaction_t5'.",
    )
    unavailable_reason: str | None = None


class ModelsResponse(BaseModel):
    forward: list[ModelInfo]
    conditions: list[ModelInfo]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    n_forward_models: int
    n_conditions_models: int


class ClassifyRequest(BaseModel):
    reactants: str = Field(..., description="Dot-separated reactant SMILES.")
    product: str | None = Field(
        default=None, description="Optional product SMILES for stricter rules."
    )


class ClassifyResponse(BaseModel):
    reaction_class: str
    canonical_reactants: str
    canonical_product: str | None
    source: str = Field(default="", description="The rule set that assigned the class.")
