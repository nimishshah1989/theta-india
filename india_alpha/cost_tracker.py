"""
cost_tracker.py
Tracks Claude API usage and enforces daily budget limits.
Shared across corporate_intelligence_scorer and gem_scorer.
"""

import structlog

log = structlog.get_logger()

# Approximate cost per Claude call (Sonnet 4.6 pricing)
# Input: ~$3/MTok, Output: ~$15/MTok
# Avg call: ~2K input tokens + ~500 output tokens ≈ $0.0135
ESTIMATED_COST_PER_CALL_USD = 0.015


class CostTracker:
    """Tracks Claude API calls and enforces budget."""

    def __init__(self, daily_budget_usd: float = 0.30):
        self.daily_budget_usd = daily_budget_usd
        self.calls_made = 0
        self.estimated_spend_usd = 0.0

    def can_call(self) -> bool:
        """Check if budget allows another Claude call."""
        return self.estimated_spend_usd + ESTIMATED_COST_PER_CALL_USD <= self.daily_budget_usd

    def record_call(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Record a Claude API call. Uses actual tokens if available, otherwise estimate."""
        self.calls_made += 1
        if input_tokens > 0 or output_tokens > 0:
            cost = (input_tokens / 1_000_000 * 3.0) + (output_tokens / 1_000_000 * 15.0)
        else:
            cost = ESTIMATED_COST_PER_CALL_USD
        self.estimated_spend_usd += cost

    @property
    def budget_remaining_usd(self) -> float:
        return max(0, self.daily_budget_usd - self.estimated_spend_usd)

    def summary(self) -> dict:
        return {
            "claude_calls": self.calls_made,
            "estimated_spend_usd": round(self.estimated_spend_usd, 4),
            "budget_remaining_usd": round(self.budget_remaining_usd, 4),
        }
