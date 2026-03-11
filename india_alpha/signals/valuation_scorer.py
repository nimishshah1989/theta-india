"""
valuation_scorer.py
Computes valuation_score (0-100) from market data stored in india_companies.
100% Python — no Claude, no API cost.

Five valuation dimensions that identify underpriced mid/small-cap stocks:
  1. PE vs Sector Median — relative cheapness within peer group (25 pts)
  2. Absolute PE — absolute earnings yield attractiveness (25 pts)
  3. Price-to-Book — asset-based valuation floor (15 pts)
  4. EV/EBITDA — enterprise value vs operating cash flow (20 pts)
  5. 52-Week Position — price momentum context, near-low = opportunity (15 pts)

Zone mapping applies a multiplier to the composite hidden gem score:
  DEEP_VALUE (>=75): 1.15x
  CHEAP (>=55): 1.08x
  FAIR (>=35): 1.00x
  EXPENSIVE (>=20): 0.90x
  OVERVALUED (<20): 0.75x
"""

from datetime import datetime
from statistics import median
from typing import Optional

import structlog

log = structlog.get_logger()


# ─── ZONE DEFINITIONS ──────────────────────────────────────────────────
_VALUATION_ZONES = [
    (75, "DEEP_VALUE", 1.15),
    (55, "CHEAP", 1.08),
    (35, "FAIR", 1.00),
    (20, "EXPENSIVE", 0.90),
    (0,  "OVERVALUED", 0.75),
]


def _get_valuation_zone(score: int) -> tuple:
    """Return (zone_name, multiplier) based on valuation score."""
    for threshold, zone, multiplier in _VALUATION_ZONES:
        if score >= threshold:
            return zone, multiplier
    return "OVERVALUED", 0.75


def _compute_sector_medians(companies: list[dict]) -> dict[str, float]:
    """
    Compute median trailing PE per sector from a list of company dicts.
    Only includes companies with positive PE (profitable) in the median calculation.
    Returns dict mapping sector name to median PE.
    """
    sector_pes: dict[str, list[float]] = {}

    for company in companies:
        sector = company.get("sector")
        pe = company.get("trailing_pe")
        if sector and pe is not None and pe > 0:
            sector_pes.setdefault(sector, []).append(pe)

    sector_medians = {}
    for sector, pes in sector_pes.items():
        if len(pes) >= 3:
            # Need at least 3 companies to compute a meaningful sector median
            sector_medians[sector] = median(pes)

    return sector_medians


def compute_valuation_score(company: dict, sector_median_pe: Optional[float] = None) -> dict:
    """
    Pure function. Takes a company dict (from india_companies) and optional
    sector median PE. Returns score dict ready for india_valuation_scores.

    Handles None values gracefully — missing fields score 0 for that dimension.
    If no sector median is available, the relative PE dimension is skipped
    and only the remaining 4 dimensions (out of 75 max) are scored.
    """
    trailing_pe = company.get("trailing_pe")
    price_to_book = company.get("price_to_book")
    ev_to_ebitda = company.get("ev_to_ebitda")
    fifty_two_week_low = company.get("fifty_two_week_low")
    fifty_two_week_high = company.get("fifty_two_week_high")
    current_price = company.get("current_price")
    two_hundred_dma = company.get("two_hundred_dma")

    dimension_scores = {}

    # ─── DIMENSION 1: PE vs Sector Median (25 pts) ─────────────────────
    pe_relative_score = 0
    if (
        sector_median_pe is not None
        and sector_median_pe > 0
        and trailing_pe is not None
        and trailing_pe > 0
    ):
        ratio = trailing_pe / sector_median_pe
        if ratio < 0.5:
            pe_relative_score = 25
        elif ratio < 0.75:
            pe_relative_score = 18
        elif ratio < 1.0:
            pe_relative_score = 12
        elif ratio < 1.5:
            pe_relative_score = 6
        else:
            pe_relative_score = 0
    dimension_scores["pe_relative"] = pe_relative_score

    # ─── DIMENSION 2: Absolute PE (25 pts) ─────────────────────────────
    pe_absolute_score = 0
    if trailing_pe is not None and trailing_pe > 0:
        if trailing_pe < 8:
            pe_absolute_score = 25
        elif trailing_pe < 15:
            pe_absolute_score = 18
        elif trailing_pe < 25:
            pe_absolute_score = 10
        elif trailing_pe < 40:
            pe_absolute_score = 3
        else:
            pe_absolute_score = 0
    # Negative PE (loss-making) stays at 0
    dimension_scores["pe_absolute"] = pe_absolute_score

    # ─── DIMENSION 3: Price-to-Book (15 pts) ───────────────────────────
    pb_score = 0
    if price_to_book is not None and price_to_book > 0:
        if price_to_book < 1.0:
            pb_score = 15
        elif price_to_book < 2.0:
            pb_score = 10
        elif price_to_book < 4.0:
            pb_score = 5
        elif price_to_book < 6.0:
            pb_score = 2
        else:
            pb_score = 0
    dimension_scores["pb"] = pb_score

    # ─── DIMENSION 4: EV/EBITDA (20 pts) ───────────────────────────────
    ev_ebitda_score = 0
    if ev_to_ebitda is not None and ev_to_ebitda > 0:
        if ev_to_ebitda < 6:
            ev_ebitda_score = 20
        elif ev_to_ebitda < 10:
            ev_ebitda_score = 14
        elif ev_to_ebitda < 15:
            ev_ebitda_score = 8
        elif ev_to_ebitda < 20:
            ev_ebitda_score = 3
        else:
            ev_ebitda_score = 0
    dimension_scores["ev_ebitda"] = ev_ebitda_score

    # ─── DIMENSION 5: 52-Week Position (15 pts) ────────────────────────
    fifty_two_week_score = 0
    position_pct = None
    if (
        fifty_two_week_low is not None
        and fifty_two_week_high is not None
        and current_price is not None
        and fifty_two_week_high > fifty_two_week_low
    ):
        price_range = fifty_two_week_high - fifty_two_week_low
        position_pct = ((current_price - fifty_two_week_low) / price_range) * 100

        if position_pct <= 20:
            fifty_two_week_score = 15
        elif position_pct <= 40:
            fifty_two_week_score = 10
        elif position_pct <= 60:
            fifty_two_week_score = 6
        elif position_pct <= 80:
            fifty_two_week_score = 3
        else:
            fifty_two_week_score = 0

        # Bonus: below 200-day moving average
        if two_hundred_dma is not None and current_price < two_hundred_dma:
            fifty_two_week_score += 3

    dimension_scores["fifty_two_week"] = fifty_two_week_score

    # ─── MISSING DATA HANDLING ────────────────────────────────────────
    # Count how many dimensions actually had data to score
    dim_max = {"pe_relative": 25, "pe_absolute": 25, "pb": 15, "ev_ebitda": 20, "fifty_two_week": 15}
    scored_dims = []
    for dim_name, dim_score in dimension_scores.items():
        has_data = False
        if dim_name == "pe_relative" and sector_median_pe and trailing_pe and trailing_pe > 0:
            has_data = True
        elif dim_name == "pe_absolute" and trailing_pe is not None and trailing_pe > 0:
            has_data = True
        elif dim_name == "pb" and price_to_book is not None and price_to_book > 0:
            has_data = True
        elif dim_name == "ev_ebitda" and ev_to_ebitda is not None and ev_to_ebitda > 0:
            has_data = True
        elif dim_name == "fifty_two_week" and fifty_two_week_low is not None and fifty_two_week_high is not None and current_price is not None:
            has_data = True
        if has_data:
            scored_dims.append(dim_name)

    n_scored = len(scored_dims)

    if n_scored == 0:
        # No valuation data at all — return FAIR so it doesn't crush the score
        return {
            "valuation_score": 35,
            "valuation_zone": "FAIR",
            "valuation_multiplier": 1.00,
            "dimension_scores": dimension_scores,
            "score_narrative": "Insufficient valuation data — defaulting to FAIR (1.00x)",
        }

    if n_scored <= 2:
        # Too few dimensions — impute missing at 60% of avg scored proportion
        scored_total = sum(dimension_scores[d] for d in scored_dims)
        scored_max = sum(dim_max[d] for d in scored_dims)
        avg_pct = scored_total / scored_max if scored_max > 0 else 0.5
        impute_pct = avg_pct * 0.60
        for dim_name in dimension_scores:
            if dim_name not in scored_dims:
                dimension_scores[dim_name] = round(dim_max[dim_name] * impute_pct)

    # ─── TOTAL SCORE ───────────────────────────────────────────────────
    total_score = sum(dimension_scores.values())
    total_score = max(0, min(100, total_score))

    zone, multiplier = _get_valuation_zone(total_score)

    return {
        "valuation_score": total_score,
        "valuation_zone": zone,
        "valuation_multiplier": multiplier,
        "dimension_scores": dimension_scores,
        "score_narrative": _build_valuation_narrative(
            zone, total_score, dimension_scores, company, position_pct
        ),
    }


def _build_valuation_narrative(
    zone: str,
    score: int,
    dimension_scores: dict,
    company: dict,
    position_pct: Optional[float] = None,
) -> str:
    """Build a human-readable summary of the valuation assessment."""
    parts = []
    ticker = company.get("ticker", "Unknown")
    trailing_pe = company.get("trailing_pe")
    price_to_book = company.get("price_to_book")
    ev_to_ebitda = company.get("ev_to_ebitda")

    # Lead with the strongest dimension
    scored_dims = sorted(dimension_scores.items(), key=lambda x: x[1], reverse=True)
    top_dim_name, top_dim_score = scored_dims[0] if scored_dims else ("", 0)

    if trailing_pe is not None and trailing_pe > 0 and top_dim_name in ("pe_relative", "pe_absolute"):
        parts.append(f"PE {trailing_pe:.1f}x")
    if price_to_book is not None and price_to_book > 0 and top_dim_name == "pb":
        parts.append(f"P/B {price_to_book:.1f}x")
    if ev_to_ebitda is not None and ev_to_ebitda > 0 and top_dim_name == "ev_ebitda":
        parts.append(f"EV/EBITDA {ev_to_ebitda:.1f}x")
    if position_pct is not None and top_dim_name == "fifty_two_week":
        parts.append(f"Trading at {position_pct:.0f}% of 52w range")

    # Add secondary insight
    if trailing_pe is not None and trailing_pe > 0 and "PE" not in " ".join(parts):
        parts.append(f"PE {trailing_pe:.1f}x")
    if ev_to_ebitda is not None and ev_to_ebitda > 0 and "EV/EBITDA" not in " ".join(parts):
        parts.append(f"EV/EBITDA {ev_to_ebitda:.1f}x")

    # Zone suffix
    if zone == "DEEP_VALUE":
        suffix = " -- deep value across multiple metrics, significant margin of safety"
    elif zone == "CHEAP":
        suffix = " -- attractively priced relative to fundamentals"
    elif zone == "FAIR":
        suffix = " -- reasonably valued, no margin of safety"
    elif zone == "EXPENSIVE":
        suffix = " -- premium valuation, growth must justify price"
    else:
        suffix = " -- overvalued on most metrics, limited upside potential"

    # Handle case where no valuation data is available
    if not parts:
        return f"Insufficient valuation data for {ticker}"

    return "; ".join(parts) + suffix


async def score_company_valuation(
    db,
    company: dict,
    sector_median_pe: Optional[float] = None,
) -> dict:
    """
    Score one company's valuation. Takes a company dict (already loaded),
    computes score, and upserts to india_valuation_scores.
    """
    isin = company.get("isin")
    ticker = company.get("ticker", "")

    if not isin:
        log.warning("valuation_score_skip_no_isin", ticker=ticker)
        return {"valuation_score": 0}

    score_data = compute_valuation_score(company, sector_median_pe)

    record = {
        "isin": isin,
        "ticker": ticker,
        "valuation_score": score_data["valuation_score"],
        "valuation_zone": score_data["valuation_zone"],
        "valuation_multiplier": score_data["valuation_multiplier"],
        "trailing_pe": company.get("trailing_pe"),
        "price_to_book": company.get("price_to_book"),
        "ev_to_ebitda": company.get("ev_to_ebitda"),
        "sector_median_pe": sector_median_pe,
        "dimension_scores": score_data["dimension_scores"],
        "score_narrative": score_data["score_narrative"],
        "scored_at": datetime.now().isoformat(),
    }

    await db.table("india_valuation_scores").upsert(
        record, on_conflict="isin"
    ).execute()

    log.debug(
        "valuation_scored",
        ticker=ticker,
        score=score_data["valuation_score"],
        zone=score_data["valuation_zone"],
    )
    return score_data


async def score_all_companies_valuation(db) -> dict:
    """
    Main entry point. Queries all companies from india_companies,
    computes per-sector median PE, scores each company's valuation,
    and upserts results to india_valuation_scores.
    """
    results = {"scored": 0, "skipped": 0, "errors": 0}

    # Fetch all companies (paginated to bypass 1000-row limit)
    from india_alpha.db import fetch_all_rows
    companies = await fetch_all_rows(db, "india_companies")
    if not companies:
        log.warning("valuation_scoring_no_companies")
        return results

    log.info("valuation_scoring_start", companies=len(companies))

    # Compute sector median PEs across the full universe
    sector_medians = _compute_sector_medians(companies)
    log.info(
        "sector_medians_computed",
        sectors=len(sector_medians),
        sample={k: round(v, 1) for k, v in list(sector_medians.items())[:5]},
    )

    for company in companies:
        isin = company.get("isin")
        ticker = company.get("ticker", "")

        if not isin:
            results["skipped"] += 1
            continue

        try:
            sector = company.get("sector", "")
            sector_median = sector_medians.get(sector)
            await score_company_valuation(db, company, sector_median)
            results["scored"] += 1
        except Exception as exc:
            log.error(
                "valuation_score_failed",
                ticker=ticker,
                error=str(exc)[:100],
            )
            results["errors"] += 1

    log.info("valuation_scoring_complete", **results)
    return results
