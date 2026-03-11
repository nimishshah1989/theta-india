"""API routes for JIP Horizon India — 4 endpoints serving precomputed scoring data."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from typing import Optional

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from india_alpha.api.models import (
    GemDetail,
    GemSummary,
    GemsListResponse,
    PipelineJob,
    PipelineRunResponse,
    PipelineStatusResponse,
)
from india_alpha.db import get_async_db

logger = structlog.get_logger()

router = APIRouter()

VALID_PIPELINE_STEPS = [
    "all", "universe", "enrich", "insider", "financials",
    "quality", "policy", "corporate", "valuation",
    "smartmoney", "degradation", "score", "output",
]

MAX_GEMS_LIMIT = 100
DEFAULT_GEMS_LIMIT = 20


# ─────────────────────────────────────────────────────────────
# GET /gems — List hidden gems with optional tier filter
# ─────────────────────────────────────────────────────────────

@router.get("/gems", response_model=GemsListResponse)
async def list_gems(
    tier: Optional[str] = Query(None, description="Filter by conviction tier: HIGHEST, HIGH, MEDIUM, WATCH"),
    limit: int = Query(DEFAULT_GEMS_LIMIT, ge=1, le=MAX_GEMS_LIMIT),
    offset: int = Query(0, ge=0),
):
    """Return top hidden gems sorted by final_score DESC."""
    db = await get_async_db()

    query = db.table("india_hidden_gems").select("*", count="exact")

    if tier:
        tier_upper = tier.upper()
        valid_tiers = ["HIGHEST", "HIGH", "MEDIUM", "WATCH", "BELOW_THRESHOLD"]
        if tier_upper not in valid_tiers:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid tier '{tier}'. Must be one of: {', '.join(valid_tiers)}"
            )
        query = query.eq("conviction_tier", tier_upper)

    query = query.order("final_score", desc=True).range(offset, offset + limit - 1)

    result = await query.execute()
    rows = result.data or []
    total = result.count or 0

    gems = [GemSummary(**row) for row in rows]

    return GemsListResponse(
        gems=gems,
        total=total,
        tier_filter=tier.upper() if tier else None,
        limit=limit,
        offset=offset,
    )


# ─────────────────────────────────────────────────────────────
# GET /gem/{ticker} — Full company detail with related data
# ─────────────────────────────────────────────────────────────

@router.get("/gem/{ticker}", response_model=GemDetail)
async def get_gem_detail(ticker: str):
    """Return full detail for a single company including related signals."""
    db = await get_async_db()
    ticker_upper = ticker.upper()

    # Fetch the gem record
    gem_result = await (
        db.table("india_hidden_gems")
        .select("*")
        .eq("ticker", ticker_upper)
        .execute()
    )
    gem_rows = gem_result.data or []

    if not gem_rows:
        raise HTTPException(status_code=404, detail=f"No gem found for ticker '{ticker_upper}'")

    gem = gem_rows[0]
    isin = gem.get("isin", "")

    # Parallel fetch related data from 5 tables
    company_task = (
        db.table("india_companies")
        .select("*")
        .eq("ticker", ticker_upper)
        .limit(1)
        .execute()
    )
    insider_task = (
        db.table("india_promoter_signals")
        .select("*")
        .eq("ticker", ticker_upper)
        .order("transaction_date", desc=True)
        .limit(10)
        .execute()
    )
    bulk_deals_task = (
        db.table("india_bulk_deals")
        .select("*")
        .eq("ticker", ticker_upper)
        .order("trade_date", desc=True)
        .limit(10)
        .execute()
    )
    shareholding_task = (
        db.table("india_shareholding_patterns")
        .select("*")
        .eq("isin", isin)
        .order("quarter", desc=True)
        .limit(1)
        .execute()
    ) if isin else None
    filings_task = (
        db.table("india_corporate_filings")
        .select("*")
        .eq("isin", isin)
        .order("sort_date", desc=True)
        .limit(5)
        .execute()
    ) if isin else None

    # Gather all tasks
    tasks = [company_task, insider_task, bulk_deals_task]
    if shareholding_task:
        tasks.append(shareholding_task)
    if filings_task:
        tasks.append(filings_task)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Extract results safely
    company_data = _safe_first(results[0])
    insider_data = _safe_list(results[1])
    bulk_deals_data = _safe_list(results[2])

    shareholding_data = None
    filings_data = []

    if isin and len(results) > 3:
        shareholding_data = _safe_first(results[3])
    if isin and len(results) > 4:
        filings_data = _safe_list(results[4])

    return GemDetail(
        **gem,
        company=company_data,
        recent_insider_signals=insider_data,
        recent_bulk_deals=bulk_deals_data,
        shareholding=shareholding_data,
        recent_filings=filings_data,
    )


# ─────────────────────────────────────────────────────────────
# GET /pipeline/status — Recent pipeline job runs
# ─────────────────────────────────────────────────────────────

@router.get("/pipeline/status", response_model=PipelineStatusResponse)
async def pipeline_status():
    """Return last 10 pipeline job runs."""
    db = await get_async_db()

    result = await (
        db.table("india_job_runs")
        .select("*")
        .order("started_at", desc=True)
        .limit(10)
        .execute()
    )
    rows = result.data or []

    jobs = [PipelineJob(**row) for row in rows]
    return PipelineStatusResponse(jobs=jobs)


# ─────────────────────────────────────────────────────────────
# POST /pipeline/run — Trigger pipeline in background
# ─────────────────────────────────────────────────────────────

@router.post("/pipeline/run", response_model=PipelineRunResponse)
async def trigger_pipeline(
    step: str = Query("all", description="Pipeline step to run"),
    background_tasks: BackgroundTasks = None,
):
    """Trigger a pipeline run as a background subprocess."""
    if step not in VALID_PIPELINE_STEPS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid step '{step}'. Must be one of: {', '.join(VALID_PIPELINE_STEPS)}"
        )

    db = await get_async_db()

    # Guard: prevent concurrent runs
    running_check = await (
        db.table("india_job_runs")
        .select("id")
        .eq("status", "running")
        .limit(1)
        .execute()
    )
    if running_check.data:
        raise HTTPException(
            status_code=409,
            detail="A pipeline run is already in progress. Wait for it to complete."
        )

    # Generate job ID and insert initial record
    job_id = str(uuid.uuid4())

    await (
        db.table("india_job_runs")
        .insert({
            "id": job_id,
            "job_name": f"api_triggered_{step}",
            "status": "running",
        })
        .execute()
    )

    # Launch pipeline as subprocess (isolated from API event loop)
    subprocess.Popen(
        [sys.executable, "scripts/run_pipeline.py", "--step", step],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    logger.info("pipeline_triggered", step=step, job_id=job_id)

    return PipelineRunResponse(
        job_id=job_id,
        status="running",
        message=f"Pipeline step '{step}' started in background",
    )


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _safe_first(result):
    """Extract first row from a Supabase result, or None on error."""
    if isinstance(result, Exception):
        logger.warning("query_failed", error=str(result))
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def _safe_list(result):
    """Extract rows list from a Supabase result, or empty list on error."""
    if isinstance(result, Exception):
        logger.warning("query_failed", error=str(result))
        return []
    return getattr(result, "data", None) or []
