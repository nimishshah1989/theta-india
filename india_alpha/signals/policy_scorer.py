"""
policy_scorer.py
Computes policy_tailwind_score (0-100) by matching company sector/industry
against the government policy registry.
100% Python — no Claude, no API cost.

Approach:
  1. Load policy_registry.json (curated policy-sector mapping)
  2. For each company, match sector + industry against policy beneficiaries
  3. Score = sum of matching policy impacts (HIGH=15, MEDIUM=10, LOW=5), capped at 100
  4. Fuzzy matching handles yfinance sector name variations
"""

import json
import os
import asyncio
import structlog
from datetime import datetime
from typing import Optional

log = structlog.get_logger()

# Impact score mapping
IMPACT_SCORES = {
    "HIGH": 15,
    "MEDIUM": 10,
    "LOW": 5,
}

# Path to policy registry
_REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "policy_registry.json"
)

# Cache loaded registry
_registry_cache: Optional[dict] = None


def _load_registry() -> list[dict]:
    """Load and cache the policy registry."""
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache

    with open(_REGISTRY_PATH, "r") as f:
        data = json.load(f)
    _registry_cache = data["policies"]
    return _registry_cache


def _normalize(text: str) -> str:
    """Lowercase and strip for fuzzy matching."""
    return (text or "").lower().strip()


def _matches_policy(sector: str, industry: str, policy: dict) -> bool:
    """
    Check if a company's sector/industry matches a policy's beneficiaries.
    Uses both exact sector match and keyword matching against sector + industry.
    """
    sector_norm = _normalize(sector)
    industry_norm = _normalize(industry)
    combined = f"{sector_norm} {industry_norm}"

    # Check exact sector match (case-insensitive)
    for beneficiary_sector in policy.get("beneficiary_sectors", []):
        if _normalize(beneficiary_sector) in sector_norm or sector_norm in _normalize(beneficiary_sector):
            return True

    # Check keyword match against combined sector + industry
    for keyword in policy.get("beneficiary_keywords", []):
        if _normalize(keyword) in combined:
            return True

    return False


def compute_policy_score(sector: str, industry: str) -> dict:
    """
    Pure function. Takes company sector/industry strings.
    Returns score dict ready for india_policy_scores.
    """
    if not sector and not industry:
        return _empty_score()

    policies = _load_registry()
    matching = []

    for policy in policies:
        if _matches_policy(sector, industry, policy):
            impact = policy.get("impact", "LOW")
            matching.append({
                "policy_id": policy["id"],
                "policy_name": policy["name"],
                "impact": impact,
                "points": IMPACT_SCORES.get(impact, 5),
            })

    raw_score = sum(m["points"] for m in matching)
    final_score = max(0, min(100, raw_score))

    return {
        "policy_score": final_score,
        "matching_policies": matching,
        "score_narrative": _build_policy_narrative(matching, final_score, sector),
    }


def _build_policy_narrative(matching: list, score: int, sector: str) -> str:
    if not matching:
        return f"No active government policy tailwinds identified for {sector or 'this sector'}"

    policy_names = [m["policy_name"] for m in matching[:3]]
    names_str = "; ".join(policy_names)

    if score >= 30:
        suffix = " → Strong government tailwind — multiple active policies"
    elif score >= 15:
        suffix = " → Moderate policy support"
    else:
        suffix = " → Minor policy relevance"

    return f"Beneficiary of: {names_str}{suffix}"


def _empty_score() -> dict:
    return {
        "policy_score": 0,
        "matching_policies": [],
        "score_narrative": "No sector data available for policy matching",
    }


async def score_company_policy(db, isin: str, ticker: str) -> dict:
    """Score one company's policy tailwind. Reads sector from DB, writes score."""
    result = await db.table("india_companies") \
        .select("sector, industry") \
        .eq("isin", isin) \
        .execute()

    if not result.data:
        # Try by ticker as fallback
        result = await db.table("india_companies") \
            .select("sector, industry") \
            .eq("ticker", ticker) \
            .execute()

    company = result.data[0] if result.data else {}
    sector = company.get("sector", "")
    industry = company.get("industry", "")

    score_data = compute_policy_score(sector, industry)

    record = {
        "isin": isin,
        "ticker": ticker,
        **score_data,
        "scored_at": datetime.now().isoformat(),
    }

    await db.table("india_policy_scores").upsert(
        record, on_conflict="isin"
    ).execute()

    log.debug("policy_scored", ticker=ticker, score=score_data["policy_score"],
              policies=len(score_data["matching_policies"]))
    return score_data


async def score_all_companies_policy(db) -> dict:
    """Score policy tailwind for all companies in universe."""
    results = {"scored": 0, "no_sector": 0, "errors": 0}

    from india_alpha.db import fetch_all_rows
    companies = await fetch_all_rows(db, "india_companies", select="isin, ticker, sector")
    log.info("policy_scoring_start", companies=len(companies))

    for company in companies:
        try:
            if not company.get("sector"):
                results["no_sector"] += 1
                continue
            await score_company_policy(db, company["isin"], company["ticker"])
            results["scored"] += 1
        except Exception as e:
            log.error("policy_score_failed",
                      ticker=company.get("ticker"), error=str(e)[:100])
            results["errors"] += 1

    log.info("policy_scoring_complete", **results)
    return results
