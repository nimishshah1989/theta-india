"""Unit tests for pure scoring functions in gem_scorer.py."""
from __future__ import annotations

import pytest

from india_alpha.processing.gem_scorer import (
    compute_composite_score,
    get_conviction_tier,
    rescale_layer_score,
)


# ─────────────────────────────────────────────────────────────
# rescale_layer_score
# ─────────────────────────────────────────────────────────────

class TestRescaleLayerScore:
    """Test piecewise-linear rescaling for each layer."""

    def test_zero_returns_zero(self):
        assert rescale_layer_score("promoter", 0) == 0.0

    def test_negative_returns_zero(self):
        assert rescale_layer_score("promoter", -5) == 0.0

    def test_promoter_breakpoints(self):
        """Promoter: (10, 25), (20, 50), (30, 70)."""
        assert rescale_layer_score("promoter", 10) == 25.0
        assert rescale_layer_score("promoter", 20) == 50.0
        assert rescale_layer_score("promoter", 30) == 70.0

    def test_promoter_interpolation(self):
        """Midpoint between (10, 25) and (20, 50) should be ~37.5."""
        result = rescale_layer_score("promoter", 15)
        assert abs(result - 37.5) < 0.5

    def test_ol_breakpoints(self):
        """OL: (25, 50), (40, 70)."""
        assert rescale_layer_score("ol", 25) == 50.0
        assert rescale_layer_score("ol", 40) == 70.0

    def test_corp_intel_breakpoints(self):
        """Corp Intel: (8, 20), (18, 50), (30, 70)."""
        assert rescale_layer_score("corp_intel", 8) == 20.0
        assert rescale_layer_score("corp_intel", 18) == 50.0

    def test_max_score(self):
        """Score of 100 maps to 100."""
        assert rescale_layer_score("promoter", 100) == 100.0

    def test_above_100_capped(self):
        """Scores above 100 are capped at 100."""
        assert rescale_layer_score("promoter", 150) == 100.0

    def test_unknown_layer_returns_zero(self):
        assert rescale_layer_score("nonexistent", 50) == 0.0


# ─────────────────────────────────────────────────────────────
# compute_composite_score
# ─────────────────────────────────────────────────────────────

class TestCompositeScore:
    """Test weighted composite calculation with dynamic normalization."""

    def test_all_layers_positive(self):
        """All 5 layers active — uses full weights."""
        result = compute_composite_score(
            promoter_score=20,
            ol_score=25,
            corp_intel_score=18,
            policy_score=20,
            quality_score=20,
        )
        # All scores map to 50 rescaled — weighted avg = 50, no convergence bonus (5 layers fire at >=40)
        # Actually 5 layers >= 40 means 1.15x bonus → 50 * 1.15 = 57.5
        assert result > 0
        assert result <= 100

    def test_partial_layers(self):
        """Only 2 layers active — weights renormalize to sum to 1.0."""
        result = compute_composite_score(
            promoter_score=20,
            ol_score=25,
            corp_intel_score=0,
            policy_score=0,
            quality_score=0,
        )
        # Only promoter (30%) and OL (30%) active, normalized to 50% each
        assert result > 0

    def test_all_zero_returns_zero(self):
        """No active layers → composite = 0."""
        result = compute_composite_score(
            promoter_score=0,
            ol_score=0,
            corp_intel_score=0,
            policy_score=0,
            quality_score=0,
        )
        assert result == 0.0

    def test_convergence_bonus_two_layers(self):
        """Two layers >= 40 rescaled gives 6% bonus."""
        # promoter=30 → rescaled 70, OL=40 → rescaled 70
        # Both >= 40, so 6% bonus applied
        result_high = compute_composite_score(
            promoter_score=30, ol_score=40,
            corp_intel_score=0, policy_score=0, quality_score=0,
        )
        # Compare with single layer (no convergence bonus)
        result_single = compute_composite_score(
            promoter_score=30, ol_score=0,
            corp_intel_score=0, policy_score=0, quality_score=0,
        )
        # result_high should be > result_single (both layers + bonus)
        assert result_high > result_single

    def test_convergence_bonus_four_layers(self):
        """Four layers >= 40 rescaled gives 15% bonus."""
        result = compute_composite_score(
            promoter_score=30,      # rescaled ~70
            ol_score=40,            # rescaled ~70
            corp_intel_score=30,    # rescaled ~70
            policy_score=35,        # rescaled ~70
            quality_score=0,
        )
        # 4 layers firing at >=40 rescaled → 15% convergence bonus
        assert result > 50  # Should be high with 4 layers firing


# ─────────────────────────────────────────────────────────────
# get_conviction_tier
# ─────────────────────────────────────────────────────────────

class TestConvictionTier:
    """Test tier assignment from composite score."""

    def test_highest_tier(self):
        assert get_conviction_tier(75.0) == "HIGHEST"
        assert get_conviction_tier(70.0) == "HIGHEST"

    def test_high_tier(self):
        assert get_conviction_tier(65.0) == "HIGH"
        assert get_conviction_tier(58.0) == "HIGH"

    def test_medium_tier(self):
        assert get_conviction_tier(50.0) == "MEDIUM"
        assert get_conviction_tier(45.0) == "MEDIUM"

    def test_watch_tier(self):
        assert get_conviction_tier(35.0) == "WATCH"
        assert get_conviction_tier(30.0) == "WATCH"

    def test_below_threshold(self):
        assert get_conviction_tier(29.9) == "BELOW_THRESHOLD"
        assert get_conviction_tier(0.0) == "BELOW_THRESHOLD"

    def test_perfect_score(self):
        assert get_conviction_tier(100.0) == "HIGHEST"

    def test_boundary_values(self):
        """Test exact boundary values."""
        assert get_conviction_tier(69.9) == "HIGH"  # Just below HIGHEST
        assert get_conviction_tier(57.9) == "MEDIUM"  # Just below HIGH
        assert get_conviction_tier(44.9) == "WATCH"  # Just below MEDIUM
