"""Test fixtures for JIP Horizon India API tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


# ─────────────────────────────────────────────────────────────
# Sample data for mocking Supabase responses
# ─────────────────────────────────────────────────────────────

SAMPLE_GEM = {
    "id": "test-uuid-1",
    "isin": "INE001A01036",
    "ticker": "TESTCO",
    "company_name": "Test Company Ltd",
    "exchange": "NSE",
    "market_cap_cr": 1500.0,
    "analyst_count": 2,
    "promoter_score": 45,
    "operating_leverage_score": 38,
    "concall_score": 62,
    "policy_tailwind_score": 28,
    "quality_emergence_score": 15,
    "valuation_multiplier": 1.05,
    "smart_money_bonus": 8,
    "degradation_penalty": -5,
    "base_composite": 52.3,
    "final_score": 57.2,
    "conviction_tier": "MEDIUM",
    "layers_firing": 3,
    "is_degrading": False,
    "is_pre_discovery": True,
    "is_below_institutional": True,
    "gem_thesis": None,
    "key_catalyst": None,
    "catalyst_timeline": None,
    "catalyst_confidence": None,
    "primary_risk": None,
    "what_market_misses": None,
    "entry_note": None,
    "scored_at": "2026-03-11T10:00:00Z",
    "last_updated": "2026-03-11T10:00:00Z",
}

SAMPLE_COMPANY = {
    "id": "comp-uuid-1",
    "ticker": "TESTCO",
    "company_name": "Test Company Ltd",
    "sector": "IT",
    "industry": "Software",
    "market_cap_cr": 1500.0,
    "trailing_pe": 22.5,
    "price_to_book": 3.1,
}

SAMPLE_JOB = {
    "id": "job-uuid-1",
    "job_name": "pipeline_score",
    "status": "success",
    "started_at": "2026-03-11T10:00:00Z",
    "completed_at": "2026-03-11T10:15:00Z",
    "records_processed": 287,
    "claude_calls_made": 42,
    "cost_usd": 0.28,
    "error_msg": None,
    "details": {},
}


def _make_mock_result(data, count=None):
    """Create a mock Supabase query result."""
    result = MagicMock()
    result.data = data
    result.count = count if count is not None else len(data)
    return result


def _make_chainable_query(return_data, count=None):
    """Create a mock Supabase query chain (select → eq → order → limit → execute)."""
    mock_result = _make_mock_result(return_data, count)
    query = MagicMock()
    # Every chained method returns the same query, except execute
    for method_name in ("select", "eq", "neq", "gt", "gte", "lt", "lte",
                        "order", "limit", "range", "in_"):
        getattr(query, method_name).return_value = query
    query.execute = AsyncMock(return_value=mock_result)
    return query


@pytest.fixture
def mock_db():
    """Patch get_async_db to return a mock Supabase client."""
    mock_client = MagicMock()

    def table_router(table_name):
        if table_name == "india_hidden_gems":
            return _make_chainable_query([SAMPLE_GEM], count=1)
        elif table_name == "india_companies":
            return _make_chainable_query([SAMPLE_COMPANY])
        elif table_name == "india_promoter_signals":
            return _make_chainable_query([])
        elif table_name == "india_bulk_deals":
            return _make_chainable_query([])
        elif table_name == "india_shareholding_patterns":
            return _make_chainable_query([])
        elif table_name == "india_corporate_filings":
            return _make_chainable_query([])
        elif table_name == "india_job_runs":
            return _make_chainable_query([SAMPLE_JOB])
        else:
            return _make_chainable_query([])

    mock_client.table = MagicMock(side_effect=table_router)

    with patch("india_alpha.api.routes.get_async_db", new=AsyncMock(return_value=mock_client)):
        with patch("india_alpha.db.get_async_db", new=AsyncMock(return_value=mock_client)):
            yield mock_client


@pytest.fixture
async def client(mock_db):
    """Async HTTP client for testing FastAPI endpoints."""
    from main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
