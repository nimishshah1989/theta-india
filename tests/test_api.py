"""API endpoint tests for JIP Horizon India."""
from __future__ import annotations

import pytest


# ─────────────────────────────────────────────────────────────
# Health Check
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health(client):
    """GET /health returns 200 with expected structure."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "version" in data
    assert "environment" in data
    assert "db_connected" in data


# ─────────────────────────────────────────────────────────────
# GET /api/v1/gems
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gems_list_default(client):
    """GET /api/v1/gems returns 200 with valid list structure."""
    resp = await client.get("/api/v1/gems")
    assert resp.status_code == 200
    data = resp.json()
    assert "gems" in data
    assert "total" in data
    assert isinstance(data["gems"], list)
    assert data["limit"] == 20
    assert data["offset"] == 0


@pytest.mark.asyncio
async def test_gems_list_with_tier_filter(client):
    """GET /api/v1/gems?tier=HIGH filters by tier."""
    resp = await client.get("/api/v1/gems?tier=HIGH")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier_filter"] == "HIGH"


@pytest.mark.asyncio
async def test_gems_list_limit(client):
    """GET /api/v1/gems?limit=5 respects the limit parameter."""
    resp = await client.get("/api/v1/gems?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 5


@pytest.mark.asyncio
async def test_gems_list_invalid_tier(client):
    """GET /api/v1/gems?tier=INVALID returns 422."""
    resp = await client.get("/api/v1/gems?tier=INVALID")
    assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────
# GET /api/v1/gem/{ticker}
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gem_detail_found(client):
    """GET /api/v1/gem/TESTCO returns 200 with full detail."""
    resp = await client.get("/api/v1/gem/TESTCO")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ticker"] == "TESTCO"
    assert data["company_name"] == "Test Company Ltd"
    assert "company" in data
    assert "recent_insider_signals" in data
    assert "recent_bulk_deals" in data


@pytest.mark.asyncio
async def test_gem_detail_not_found(client, mock_db):
    """GET /api/v1/gem/NONEXISTENT returns 404."""
    from unittest.mock import AsyncMock, MagicMock

    # Override the hidden_gems table to return empty for NONEXISTENT
    original_table = mock_db.table

    def table_with_empty_gems(table_name):
        if table_name == "india_hidden_gems":
            query = MagicMock()
            result = MagicMock()
            result.data = []
            result.count = 0
            for method in ("select", "eq", "order", "limit", "range"):
                getattr(query, method).return_value = query
            query.execute = AsyncMock(return_value=result)
            return query
        return original_table(table_name)

    mock_db.table = MagicMock(side_effect=table_with_empty_gems)

    resp = await client.get("/api/v1/gem/NONEXISTENT")
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────
# GET /api/v1/pipeline/status
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pipeline_status(client):
    """GET /api/v1/pipeline/status returns 200 with job list."""
    resp = await client.get("/api/v1/pipeline/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "jobs" in data
    assert isinstance(data["jobs"], list)


# ─────────────────────────────────────────────────────────────
# POST /api/v1/pipeline/run
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pipeline_run_invalid_step(client):
    """POST /api/v1/pipeline/run?step=invalid returns 422."""
    resp = await client.post("/api/v1/pipeline/run?step=invalid")
    assert resp.status_code == 422
