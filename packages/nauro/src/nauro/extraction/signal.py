"""Multi-dimensional signal scoring for extraction pipeline.

Replaces the single 0.0-1.0 signal_score with five orthogonal dimensions
that are combined into a weighted composite score.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from nauro.constants import (
    DEFAULT_SIGNAL_THRESHOLD,
    NAURO_SIGNAL_THRESHOLD_ENV,
    SIGNAL_WEIGHT_ARCHITECTURAL,
    SIGNAL_WEIGHT_NOVELTY,
    SIGNAL_WEIGHT_RATIONALE_DENSITY,
    SIGNAL_WEIGHT_REVERSIBILITY,
    SIGNAL_WEIGHT_SCOPE,
)


@dataclass
class SignalScore:
    """Multi-dimensional signal score for an extraction result."""

    architectural_significance: float = 0.0
    novelty: float = 0.0
    rationale_density: float = 0.0
    reversibility: float = 0.0
    scope: float = 0.0
    composite_score: float = 0.0
    reasoning: str = ""

    def to_dict(self) -> dict:
        """Serialize to a dict matching the extraction schema."""
        return {
            "architectural_significance": self.architectural_significance,
            "novelty": self.novelty,
            "rationale_density": self.rationale_density,
            "reversibility": self.reversibility,
            "scope": self.scope,
        }


def compute_composite(signal: SignalScore) -> float:
    """Compute the weighted composite score from signal dimensions.

    Formula: (arch * 0.3) + (novelty * 0.2) + (rationale * 0.2)
             + (reversibility * 0.15) + (scope * 0.15)

    Args:
        signal: A SignalScore with dimension values populated.

    Returns:
        Composite score between 0.0 and 1.0.
    """
    score = (
        signal.architectural_significance * SIGNAL_WEIGHT_ARCHITECTURAL
        + signal.novelty * SIGNAL_WEIGHT_NOVELTY
        + signal.rationale_density * SIGNAL_WEIGHT_RATIONALE_DENSITY
        + signal.reversibility * SIGNAL_WEIGHT_REVERSIBILITY
        + signal.scope * SIGNAL_WEIGHT_SCOPE
    )
    return min(max(score, 0.0), 1.0)


def should_extract(signal: SignalScore, threshold: float | None = None) -> bool:
    """Determine whether to extract based on composite score and threshold.

    Args:
        signal: A SignalScore with composite_score already computed.
        threshold: Minimum score to extract. Defaults to NAURO_SIGNAL_THRESHOLD
            env var or DEFAULT_SIGNAL_THRESHOLD.

    Returns:
        True if the signal score meets the threshold.
    """
    if threshold is None:
        threshold = float(os.environ.get(NAURO_SIGNAL_THRESHOLD_ENV, str(DEFAULT_SIGNAL_THRESHOLD)))
    return signal.composite_score >= threshold


def from_dict(data: dict) -> SignalScore:
    """Parse a SignalScore from the extraction result dict.

    Args:
        data: The full extraction result containing 'signal' and 'composite_score' keys.

    Returns:
        Populated SignalScore instance.
    """
    signal_data = data.get("signal", {})
    score = SignalScore(
        architectural_significance=signal_data.get("architectural_significance", 0.0),
        novelty=signal_data.get("novelty", 0.0),
        rationale_density=signal_data.get("rationale_density", 0.0),
        reversibility=signal_data.get("reversibility", 0.0),
        scope=signal_data.get("scope", 0.0),
        composite_score=data.get("composite_score", 0.0),
        reasoning=data.get("reasoning", ""),
    )
    return score
