"""Pydantic response models for JIP Horizon India API."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

# ─────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str
    db_connected: bool


# ─────────────────────────────────────────────────────────────
# Gem Models
# ─────────────────────────────────────────────────────────────

class GemSummary(BaseModel):
    """Lightweight gem for list views."""
    model_config = ConfigDict(from_attributes=True)

    isin: Optional[str] = None
    ticker: str
    company_name: str
    exchange: str = "NSE"
    market_cap_cr: Optional[float] = None
    analyst_count: int = 0

    # Layer scores
    promoter_score: Optional[int] = None
    operating_leverage_score: Optional[int] = None
    concall_score: Optional[int] = None
    policy_tailwind_score: Optional[int] = None
    quality_emergence_score: Optional[int] = None

    # Modifiers
    valuation_multiplier: float = 1.0
    smart_money_bonus: int = 0
    degradation_penalty: int = 0

    # Composite
    base_composite: Optional[float] = None
    final_score: Optional[float] = None
    conviction_tier: Optional[str] = None
    layers_firing: int = 0
    is_degrading: bool = False

    # Thesis snippet
    gem_thesis: Optional[str] = None
    key_catalyst: Optional[str] = None

    scored_at: Optional[str] = None


class GemDetail(GemSummary):
    """Full gem detail with related data for single-company view."""

    # Extended thesis fields
    catalyst_timeline: Optional[str] = None
    catalyst_confidence: Optional[str] = None
    primary_risk: Optional[str] = None
    what_market_misses: Optional[str] = None
    entry_note: Optional[str] = None

    # Discovery flags
    is_pre_discovery: bool = False
    is_below_institutional: bool = False

    # Related data from other tables
    company: Optional[Dict[str, Any]] = None
    recent_insider_signals: List[Dict[str, Any]] = []
    recent_bulk_deals: List[Dict[str, Any]] = []
    shareholding: Optional[Dict[str, Any]] = None
    recent_filings: List[Dict[str, Any]] = []


class GemsListResponse(BaseModel):
    """Paginated list of gems."""
    gems: List[GemSummary]
    total: int
    tier_filter: Optional[str] = None
    limit: int
    offset: int


# ─────────────────────────────────────────────────────────────
# Pipeline Models
# ─────────────────────────────────────────────────────────────

class PipelineJob(BaseModel):
    """Single pipeline job run record."""
    model_config = ConfigDict(from_attributes=True)

    id: Optional[str] = None
    job_name: str
    status: str = "running"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    records_processed: int = 0
    claude_calls_made: int = 0
    cost_usd: float = 0.0
    error_msg: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class PipelineStatusResponse(BaseModel):
    """List of recent pipeline job runs."""
    jobs: List[PipelineJob]


class PipelineRunResponse(BaseModel):
    """Immediate response when triggering a pipeline run."""
    job_id: str
    status: str
    message: str
