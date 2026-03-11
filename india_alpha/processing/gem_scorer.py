"""
gem_scorer.py
Combines all 5 signal layers into a composite Hidden Gem score,
then applies 3 modifiers: valuation gate, smart money bonus, degradation penalty.

Claude is used ONLY for the synthesis narrative on HIGH/HIGHEST tier companies.
All numerical scores are pure Python.

Base score weights (all 5 layers active):
  Promoter signal:          30%
  Operating leverage:       30%
  Corporate intelligence:   25%
  Policy tailwind:          10%
  Quality emergence:         5%

Raw layer scores are rescaled via per-layer breakpoint tables before
the weighted average, stretching compressed 10-30 raw scores to the
full 0-100 range.

Modifiers (applied after base composite):
  Valuation:   0.75x to 1.15x  (multiplicative gate)
  Smart money: -10 to +15       (additive confirmation)
  Degradation: -30 to 0         (subtractive red flags)

  final = clamp(base * valuation_mult + smart_money + degradation, 0, 100)

Conviction tiers (on final score):
  HIGHEST:  score >= 70
  HIGH:     score >= 58
  MEDIUM:   score >= 45
  WATCH:    score >= 30
"""

import json
import asyncio
import structlog
from datetime import datetime
from typing import Optional
import anthropic

from india_alpha.cost_tracker import CostTracker
from india_alpha.db import fetch_all_rows

log = structlog.get_logger()

SCORE_WEIGHTS = {
    "promoter":    0.30,
    "ol":          0.30,
    "corp_intel":  0.25,
    "policy":      0.10,
    "quality":     0.05,
}

TIER_THRESHOLDS = {
    "HIGHEST": 70,
    "HIGH":    58,
    "MEDIUM":  45,
    "WATCH":   30,
}

# Piecewise-linear breakpoint tables to stretch compressed raw scores
# to the full 0-100 range. Format: list of (raw, rescaled) tuples.
LAYER_CALIBRATION = {
    "promoter":   [(0, 0), (10, 25), (20, 50), (30, 70), (50, 85), (75, 95), (100, 100)],
    "ol":         [(0, 0), (10, 20), (25, 50), (40, 70), (55, 85), (75, 95), (100, 100)],
    "corp_intel": [(0, 0), (8, 20), (18, 50), (30, 70), (45, 85), (70, 95), (100, 100)],
    "policy":     [(0, 0), (10, 25), (20, 50), (35, 70), (50, 85), (75, 95), (100, 100)],
    "quality":    [(0, 0), (10, 25), (20, 50), (30, 70), (50, 85), (75, 95), (100, 100)],
}


def rescale_layer_score(layer: str, raw_score: float) -> float:
    """
    Piecewise-linear interpolation that maps a compressed raw score
    to the full 0-100 range using per-layer breakpoint tables.
    """
    breakpoints = LAYER_CALIBRATION.get(layer)
    if not breakpoints or raw_score <= 0:
        return 0.0

    raw_score = min(raw_score, 100.0)

    # Walk through breakpoints and interpolate
    for i in range(1, len(breakpoints)):
        raw_lo, out_lo = breakpoints[i - 1]
        raw_hi, out_hi = breakpoints[i]
        if raw_score <= raw_hi:
            if raw_hi == raw_lo:
                return float(out_hi)
            t = (raw_score - raw_lo) / (raw_hi - raw_lo)
            return round(out_lo + t * (out_hi - out_lo), 1)

    return 100.0

GEM_SYNTHESIS_PROMPT = """\
You are a research analyst scoring a potential hidden gem stock on the Indian market.
The company has been flagged by quantitative signals — assess only from data provided.

Company: {company_name} ({ticker}) | ₹{market_cap_cr} Cr market cap
Analyst coverage: {analyst_count} reports (low coverage = more potential alpha)

SIGNAL SCORES:
- Promoter Signal Score: {promoter_score}/100
  {promoter_narrative}

- Operating Leverage Score: {ol_score}/100
  {ol_narrative}

- Corporate Intelligence Score: {corp_intel_score}/100
  {corp_intel_narrative}

- Policy Tailwind Score: {policy_score}/100
  {policy_narrative}

- Quality Emergence Score: {quality_score}/100
  {quality_narrative}

VALUATION CONTEXT:
  Zone: {valuation_zone} | Multiplier: {valuation_multiplier}x
  PE: {trailing_pe} | P/B: {price_to_book} | EV/EBITDA: {ev_to_ebitda}
  Smart Money Bonus: {smart_money_bonus} | Degradation: {degradation_penalty}

TOP ACTIVE SIGNALS:
{active_signals}

TASK: Write a concise investment thesis. Return ONLY valid JSON:

{{
  "gem_thesis": "<3 sentences max. What is this company, why undiscovered, what changes the narrative>",
  "key_catalyst": "<specific event that triggers re-rating — be concrete>",
  "catalyst_timeline": "<quarter/half-year: Q2FY27, H2FY26 etc>",
  "catalyst_confidence": "high|medium|low",
  "primary_risk": "<single biggest thing that breaks this thesis>",
  "what_market_misses": "<specific mispricing — what classification error is the market making>",
  "entry_note": "<price/technical/fundamental condition for optimal entry>"
}}

Be direct. If there's no compelling thesis, say so in gem_thesis.
Quality over output — a WATCH rating is more valuable than a false HIGH.
"""


def compute_composite_score(
    promoter_score: int = 0,
    ol_score: int = 0,
    corp_intel_score: int = 0,
    policy_score: int = 0,
    quality_score: int = 0,
) -> float:
    """
    Pure function — weighted composite with dynamic normalization.
    Raw scores are rescaled via per-layer breakpoint tables before
    the weighted average, so compressed 10-30 raw scores spread
    across the full 0-100 range.
    Only includes layers where company has data (raw score > 0).
    """
    raw_scores = {
        "promoter":   promoter_score or 0,
        "ol":         ol_score or 0,
        "corp_intel": corp_intel_score or 0,
        "policy":     policy_score or 0,
        "quality":    quality_score or 0,
    }

    # Rescale each layer
    rescaled = {
        layer: rescale_layer_score(layer, score)
        for layer, score in raw_scores.items()
    }

    # Dynamic normalization — only include layers with data (raw > 0)
    active_weights = {}
    for layer, raw in raw_scores.items():
        if raw > 0:
            active_weights[layer] = SCORE_WEIGHTS[layer]

    if not active_weights:
        return 0.0

    total_active = sum(active_weights.values())

    weighted_avg = sum(
        rescaled[layer] * weight
        for layer, weight in active_weights.items()
    ) / total_active

    # Convergence bonus — based on rescaled scores >= 40
    layers_firing = sum(1 for layer in active_weights if rescaled[layer] >= 40)
    if layers_firing >= 4:
        weighted_avg = min(100, weighted_avg * 1.15)   # 15% for 4+ layers
    elif layers_firing >= 3:
        weighted_avg = min(100, weighted_avg * 1.10)   # 10% for triple
    elif layers_firing >= 2:
        weighted_avg = min(100, weighted_avg * 1.06)   # 6% for double

    return round(weighted_avg, 1)


def get_conviction_tier(composite: float) -> str:
    for tier, threshold in TIER_THRESHOLDS.items():
        if composite >= threshold:
            return tier
    return "BELOW_THRESHOLD"


async def synthesise_thesis(
    claude_client: anthropic.AsyncAnthropic,
    company: dict,
    promoter: dict,
    ol: dict,
    corp_intel: dict,
    policy: dict,
    quality: dict,
    model: str = "claude-sonnet-4-6",
    cost_tracker: Optional[CostTracker] = None,
    valuation: Optional[dict] = None,
    smart_money: Optional[dict] = None,
    degradation: Optional[dict] = None,
) -> dict:
    """
    Use Claude to write the gem thesis.
    Called only for HIGH/HIGHEST tier to control cost.
    ~1,200 tokens per call ≈ $0.005 per synthesis.
    """
    active_signals = []

    if promoter.get("highest_conviction_signal"):
        active_signals.append(f"• PROMOTER: {promoter['highest_conviction_signal']}")

    for sig in (ol.get("active_signals") or [])[:2]:
        active_signals.append(f"• OL: {sig.get('description', '')} — {sig.get('implication', '')}")

    if corp_intel.get("hidden_insight"):
        active_signals.append(f"• CORP INTEL: {corp_intel['hidden_insight']}")

    for action in (corp_intel.get("key_capital_actions") or [])[:2]:
        if isinstance(action, str):
            active_signals.append(f"• CAPITAL ACTION: {action}")

    for flag in (corp_intel.get("governance_flags") or [])[:2]:
        if isinstance(flag, str):
            active_signals.append(f"• GOVERNANCE: {flag}")

    for sig in (quality.get("active_signals") or [])[:2]:
        active_signals.append(f"• QUALITY: {sig.get('description', '')}")

    for pol in (policy.get("matching_policies") or [])[:2]:
        active_signals.append(f"• POLICY: {pol.get('policy_name', '')}")

    val = valuation or {}
    sm = smart_money or {}
    deg = degradation or {}

    prompt = GEM_SYNTHESIS_PROMPT.format(
        company_name=company.get("company_name", "Unknown"),
        ticker=company.get("ticker", ""),
        market_cap_cr=company.get("market_cap_cr") or "Unknown",
        analyst_count=company.get("analyst_count") or 0,
        promoter_score=promoter.get("promoter_signal_score", 0),
        promoter_narrative=promoter.get("score_narrative", "No data"),
        ol_score=ol.get("ol_score", 0),
        ol_narrative=ol.get("score_narrative", "No data"),
        corp_intel_score=corp_intel.get("corp_intel_score", 0),
        corp_intel_narrative=corp_intel.get("score_narrative") or "No corporate intelligence data",
        policy_score=policy.get("policy_score", 0),
        policy_narrative=policy.get("score_narrative", "No policy data"),
        quality_score=quality.get("quality_score", 0),
        quality_narrative=quality.get("score_narrative", "No quality data"),
        valuation_zone=val.get("valuation_zone", "N/A"),
        valuation_multiplier=val.get("valuation_multiplier", 1.0),
        trailing_pe=val.get("trailing_pe", "N/A"),
        price_to_book=val.get("price_to_book", "N/A"),
        ev_to_ebitda=val.get("ev_to_ebitda", "N/A"),
        smart_money_bonus=sm.get("smart_money_score", 0),
        degradation_penalty=deg.get("degradation_score", 0),
        active_signals="\n".join(active_signals) or "None available",
    )

    # Budget check
    if cost_tracker and not cost_tracker.can_call():
        log.warning("thesis_budget_exhausted", ticker=company.get("ticker"))
        return _default_thesis()

    try:
        response = await claude_client.messages.create(
            model=model,
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text

        # Track cost
        if cost_tracker:
            cost_tracker.record_call(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

        # Parse JSON — with fallback for markdown code fences
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            import re
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                return json.loads(match.group())
    except Exception as e:
        log.error("synthesis_failed",
                  ticker=company.get("ticker"), error=str(e)[:100])

    return _default_thesis()


def _default_thesis() -> dict:
    return {
        "gem_thesis": "Quantitative signals positive — manual thesis review needed",
        "key_catalyst": "TBD",
        "catalyst_timeline": "TBD",
        "catalyst_confidence": "low",
        "primary_risk": "Unknown",
        "what_market_misses": "TBD",
        "entry_note": "Review manually",
    }


def _extract_corp_intel_data(corp_intel_row: dict) -> dict:
    """Map corporate intelligence table fields to internal scorer format."""
    return {
        "corp_intel_score": corp_intel_row.get("corporate_intelligence_score", 0),
        "hidden_insight": corp_intel_row.get("hidden_insight"),
        "score_narrative": corp_intel_row.get("score_narrative"),
        "management_tone": corp_intel_row.get("management_tone"),
        "forward_signals": corp_intel_row.get("key_forward_signals", []),
        "key_capital_actions": corp_intel_row.get("key_capital_actions", []),
        "governance_flags": corp_intel_row.get("governance_flags", []),
    }


async def _bulk_fetch_all_tables(db) -> dict:
    """
    Fetch all 9 scoring tables in parallel, return keyed lookup maps.
    ~9 paginated queries instead of ~9 per company.
    Returns: {table_name: {key: row_dict}}
    """
    (companies_rows, promo_rows, ol_rows, corp_intel_rows,
     policy_rows, quality_rows,
     val_rows, sm_rows, deg_rows) = await asyncio.gather(
        fetch_all_rows(db, "india_companies"),
        fetch_all_rows(db, "india_promoter_summary"),
        fetch_all_rows(db, "india_operating_leverage_scores"),
        fetch_all_rows(db, "india_corporate_intelligence_scores"),
        fetch_all_rows(db, "india_policy_scores"),
        fetch_all_rows(db, "india_quality_scores"),
        fetch_all_rows(db, "india_valuation_scores"),
        fetch_all_rows(db, "india_smart_money_scores"),
        fetch_all_rows(db, "india_degradation_flags"),
    )

    # Key companies by ticker, everything else by isin
    companies_map = {r["ticker"]: r for r in companies_rows if r.get("ticker")}
    promo_map = {r["isin"]: r for r in promo_rows if r.get("isin")}
    ol_map = {r["isin"]: r for r in ol_rows if r.get("isin")}
    corp_intel_map = {r["isin"]: r for r in corp_intel_rows if r.get("isin")}
    policy_map = {r["isin"]: r for r in policy_rows if r.get("isin")}
    quality_map = {r["isin"]: r for r in quality_rows if r.get("isin")}
    val_map = {r["isin"]: r for r in val_rows if r.get("isin")}
    sm_map = {r["isin"]: r for r in sm_rows if r.get("isin")}
    deg_map = {r["isin"]: r for r in deg_rows if r.get("isin")}

    log.info("bulk_fetch_complete",
             companies=len(companies_map),
             promoter=len(promo_map),
             ol=len(ol_map),
             corp_intel=len(corp_intel_map),
             policy=len(policy_map),
             quality=len(quality_map),
             valuation=len(val_map),
             smart_money=len(sm_map),
             degradation=len(deg_map))

    return {
        "companies": companies_map,
        "promoter": promo_map,
        "ol": ol_map,
        "corp_intel": corp_intel_map,
        "policy": policy_map,
        "quality": quality_map,
        "valuation": val_map,
        "smart_money": sm_map,
        "degradation": deg_map,
    }


def _score_company_from_cache(
    cache: dict,
    ticker: str,
    isin: str,
) -> tuple:
    """
    Pure function: compute composite score from in-memory lookup maps.
    Returns (record_dict, tier_str, layer_data_dict) — zero DB calls.
    """
    company = cache["companies"].get(ticker, {"ticker": ticker, "isin": isin})
    promoter = cache["promoter"].get(isin, {})
    ol = cache["ol"].get(isin, {})
    policy = cache["policy"].get(isin, {})
    quality = cache["quality"].get(isin, {})
    valuation = cache["valuation"].get(isin, {})
    smart_money = cache["smart_money"].get(isin, {})
    degradation = cache["degradation"].get(isin, {})

    # Corporate intelligence — single source, no legacy fallback
    corp_intel_row = cache["corp_intel"].get(isin)
    corp_intel = _extract_corp_intel_data(corp_intel_row) if corp_intel_row else {}

    # Compute base composite with all 5 layers
    p_score = promoter.get("promoter_signal_score", 0) or 0
    o_score = ol.get("ol_score", 0) or 0
    ci_score = corp_intel.get("corp_intel_score", 0) or 0
    pol_score = policy.get("policy_score", 0) or 0
    q_score = quality.get("quality_score", 0) or 0

    base_composite = compute_composite_score(
        promoter_score=p_score,
        ol_score=o_score,
        corp_intel_score=ci_score,
        policy_score=pol_score,
        quality_score=q_score,
    )

    # Apply 3 modifiers
    val_multiplier = valuation.get("valuation_multiplier", 1.0) or 1.0
    sm_bonus = smart_money.get("smart_money_score", 0) or 0
    deg_penalty = degradation.get("degradation_score", 0) or 0
    is_degrading = degradation.get("is_degrading", False)

    # final = clamp(base * valuation_mult + smart_money + degradation, 0, 100)
    final_score = max(0, min(100, round(
        base_composite * val_multiplier + sm_bonus + deg_penalty, 1
    )))

    tier = get_conviction_tier(final_score)

    layers_firing = sum(
        1 for layer, raw in [
            ("promoter", p_score), ("ol", o_score), ("corp_intel", ci_score),
            ("policy", pol_score), ("quality", q_score),
        ]
        if rescale_layer_score(layer, raw) >= 40
    )

    record = {
        "isin": isin,
        "ticker": ticker,
        "company_name": company.get("company_name", ticker),
        "exchange": company.get("exchange", "NSE"),
        "market_cap_cr": company.get("market_cap_cr"),
        "analyst_count": company.get("analyst_count", 0),
        # Raw layer scores
        "promoter_score": promoter.get("promoter_signal_score"),
        "operating_leverage_score": ol.get("ol_score"),
        "concall_score": ci_score or None,
        "policy_tailwind_score": policy.get("policy_score"),
        "quality_emergence_score": quality.get("quality_score"),
        # Modifier values applied
        "valuation_multiplier": val_multiplier,
        "smart_money_bonus": sm_bonus,
        "degradation_penalty": deg_penalty,
        # Composite output
        "base_composite": base_composite,
        "final_score": final_score,
        "conviction_tier": tier,
        # Discovery flags
        "is_pre_discovery": (company.get("analyst_count") or 0) < 3,
        "is_below_institutional": (company.get("market_cap_cr") or 0) < 2500,
        "layers_firing": layers_firing,
        "is_degrading": is_degrading,
        # Synthesis fields — filled later for HIGH/HIGHEST
        "gem_thesis": None,
        "key_catalyst": None,
        "catalyst_timeline": None,
        "catalyst_confidence": None,
        "primary_risk": None,
        "what_market_misses": None,
        "entry_note": None,
        "scored_at": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat(),
    }

    return record, tier, {
        "company": company,
        "promoter": promoter,
        "ol": ol,
        "corp_intel": corp_intel,
        "policy": policy,
        "quality": quality,
        "valuation": valuation,
        "smart_money": smart_money,
        "degradation": degradation,
    }


async def score_and_store_gem(
    db,
    ticker: str,
    isin: str,
    claude_client: Optional[anthropic.AsyncAnthropic] = None,
    model: str = "claude-sonnet-4-6",
    cost_tracker: Optional[CostTracker] = None,
) -> Optional[dict]:
    """Full composite scoring for one company. Main entry point."""

    # Parallel DB queries — all 8 lookups at once (5 layers + 3 modifiers)
    (comp_result, promo_result, ol_result, corp_intel_result,
     policy_result, quality_result,
     val_result, sm_result, deg_result) = await asyncio.gather(
        db.table("india_companies").select("*").eq("ticker", ticker).execute(),
        db.table("india_promoter_summary").select("*").eq("isin", isin).execute(),
        db.table("india_operating_leverage_scores").select("*").eq("isin", isin).execute(),
        db.table("india_corporate_intelligence_scores").select("*").eq("isin", isin).execute(),
        db.table("india_policy_scores").select("*").eq("isin", isin).execute(),
        db.table("india_quality_scores").select("*").eq("isin", isin).execute(),
        db.table("india_valuation_scores").select("*").eq("isin", isin).execute(),
        db.table("india_smart_money_scores").select("*").eq("isin", isin).execute(),
        db.table("india_degradation_flags").select("*").eq("isin", isin).execute(),
    )

    company = comp_result.data[0] if comp_result.data else {"ticker": ticker, "isin": isin}
    promoter = promo_result.data[0] if promo_result.data else {}
    ol = ol_result.data[0] if ol_result.data else {}
    policy = policy_result.data[0] if policy_result.data else {}
    quality = quality_result.data[0] if quality_result.data else {}
    valuation = val_result.data[0] if val_result.data else {}
    smart_money = sm_result.data[0] if sm_result.data else {}
    degradation = deg_result.data[0] if deg_result.data else {}

    # Corporate intelligence — single source
    corp_intel = _extract_corp_intel_data(corp_intel_result.data[0]) if corp_intel_result.data else {}

    # Compute base composite with all 5 layers
    p_score = promoter.get("promoter_signal_score", 0) or 0
    o_score = ol.get("ol_score", 0) or 0
    ci_score = corp_intel.get("corp_intel_score", 0) or 0
    pol_score = policy.get("policy_score", 0) or 0
    q_score = quality.get("quality_score", 0) or 0

    base_composite = compute_composite_score(
        promoter_score=p_score,
        ol_score=o_score,
        corp_intel_score=ci_score,
        policy_score=pol_score,
        quality_score=q_score,
    )

    # Apply 3 modifiers
    val_multiplier = valuation.get("valuation_multiplier", 1.0) or 1.0
    sm_bonus = smart_money.get("smart_money_score", 0) or 0
    deg_penalty = degradation.get("degradation_score", 0) or 0
    is_degrading = degradation.get("is_degrading", False)

    # final = clamp(base * valuation_mult + smart_money + degradation, 0, 100)
    final_score = max(0, min(100, round(
        base_composite * val_multiplier + sm_bonus + deg_penalty, 1
    )))

    tier = get_conviction_tier(final_score)

    # Claude synthesis only for HIGH+ tier (budget-checked)
    synthesis = {}
    if tier in ("HIGHEST", "HIGH") and claude_client:
        synthesis = await synthesise_thesis(
            claude_client, company, promoter, ol,
            corp_intel, policy, quality, model, cost_tracker,
            valuation=valuation, smart_money=smart_money,
            degradation=degradation,
        )

    layers_firing = sum(
        1 for layer, raw in [
            ("promoter", p_score), ("ol", o_score), ("corp_intel", ci_score),
            ("policy", pol_score), ("quality", q_score),
        ]
        if rescale_layer_score(layer, raw) >= 40
    )

    record = {
        "isin": isin,
        "ticker": ticker,
        "company_name": company.get("company_name", ticker),
        "exchange": company.get("exchange", "NSE"),
        "market_cap_cr": company.get("market_cap_cr"),
        "analyst_count": company.get("analyst_count", 0),
        # Raw layer scores
        "promoter_score": promoter.get("promoter_signal_score"),
        "operating_leverage_score": ol.get("ol_score"),
        "concall_score": ci_score or None,
        "policy_tailwind_score": policy.get("policy_score"),
        "quality_emergence_score": quality.get("quality_score"),
        # Modifier values applied
        "valuation_multiplier": val_multiplier,
        "smart_money_bonus": sm_bonus,
        "degradation_penalty": deg_penalty,
        # Composite output
        "base_composite": base_composite,
        "final_score": final_score,
        "conviction_tier": tier,
        # Discovery flags
        "is_pre_discovery": (company.get("analyst_count") or 0) < 3,
        "is_below_institutional": (company.get("market_cap_cr") or 0) < 2500,
        "layers_firing": layers_firing,
        "is_degrading": is_degrading,
        # Synthesis fields
        "gem_thesis": synthesis.get("gem_thesis"),
        "key_catalyst": synthesis.get("key_catalyst"),
        "catalyst_timeline": synthesis.get("catalyst_timeline"),
        "catalyst_confidence": synthesis.get("catalyst_confidence"),
        "primary_risk": synthesis.get("primary_risk"),
        "what_market_misses": synthesis.get("what_market_misses"),
        "entry_note": synthesis.get("entry_note"),
        "scored_at": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat(),
    }

    await db.table("india_hidden_gems").upsert(
        record, on_conflict="isin"
    ).execute()

    log.info("gem_scored",
             ticker=ticker,
             final=final_score,
             base=base_composite,
             val_mult=val_multiplier,
             sm=sm_bonus,
             deg=deg_penalty,
             tier=tier,
             layers=layers_firing)

    return record


async def run_full_scoring(
    db,
    claude_client: Optional[anthropic.AsyncAnthropic] = None,
    model: str = "claude-sonnet-4-6",
    min_score: int = 20,
    cost_tracker: Optional[CostTracker] = None,
) -> dict:
    """
    Score all companies that have at least one signal layer fired above threshold.
    Optimized: bulk-fetches all tables once (~9 queries), scores in-memory,
    then batch-upserts results. ~10,000 DB calls → ~12.
    """
    results = {"scored": 0, "high_conviction": 0, "errors": 0}

    # Step 1: Bulk fetch all 9 tables in parallel
    cache = await _bulk_fetch_all_tables(db)

    # Step 2: Collect candidates from cache — union all layers above min_score
    seen = set()
    candidates = []

    for isin, row in cache["promoter"].items():
        if (row.get("promoter_signal_score", 0) or 0) >= min_score and isin not in seen:
            seen.add(isin)
            candidates.append({"isin": isin, "ticker": row.get("ticker", "")})

    for isin, row in cache["ol"].items():
        if (row.get("ol_score", 0) or 0) >= min_score and isin not in seen:
            seen.add(isin)
            candidates.append({"isin": isin, "ticker": row.get("ticker", "")})

    for isin, row in cache["quality"].items():
        if (row.get("quality_score", 0) or 0) >= min_score and isin not in seen:
            seen.add(isin)
            candidates.append({"isin": isin, "ticker": row.get("ticker", "")})

    for isin, row in cache["policy"].items():
        if (row.get("policy_score", 0) or 0) >= min_score and isin not in seen:
            seen.add(isin)
            candidates.append({"isin": isin, "ticker": row.get("ticker", "")})

    for isin, row in cache["corp_intel"].items():
        if (row.get("corporate_intelligence_score", 0) or 0) >= min_score and isin not in seen:
            seen.add(isin)
            candidates.append({"isin": isin, "ticker": row.get("ticker", "")})

    # DEEP_VALUE / CHEAP valuation candidates
    for isin, row in cache["valuation"].items():
        if row.get("valuation_zone") in ("DEEP_VALUE", "CHEAP") and isin not in seen:
            seen.add(isin)
            candidates.append({"isin": isin, "ticker": row.get("ticker", "")})

    log.info("composite_scoring_start", candidates=len(candidates))

    # Step 3: Score all candidates in-memory (pure Python, no DB)
    batch = []
    high_tier_items = []

    for cand in candidates:
        try:
            record, tier, layer_data = _score_company_from_cache(
                cache, cand["ticker"], cand["isin"]
            )
            batch.append(record)
            results["scored"] += 1

            if tier in ("HIGHEST", "HIGH"):
                results["high_conviction"] += 1
                high_tier_items.append((record, layer_data))

        except Exception as e:
            log.error("gem_score_failed",
                      ticker=cand.get("ticker"), error=str(e)[:500])
            results["errors"] += 1

    # Step 4: Claude synthesis for HIGH/HIGHEST only (API-bound, sequential)
    if claude_client and high_tier_items:
        log.info("claude_synthesis_start", count=len(high_tier_items))
        for record, ld in high_tier_items:
            try:
                synthesis = await synthesise_thesis(
                    claude_client,
                    ld["company"], ld["promoter"], ld["ol"],
                    ld["corp_intel"], ld["policy"], ld["quality"],
                    model, cost_tracker,
                    valuation=ld["valuation"],
                    smart_money=ld["smart_money"],
                    degradation=ld["degradation"],
                )
                record["gem_thesis"] = synthesis.get("gem_thesis")
                record["key_catalyst"] = synthesis.get("key_catalyst")
                record["catalyst_timeline"] = synthesis.get("catalyst_timeline")
                record["catalyst_confidence"] = synthesis.get("catalyst_confidence")
                record["primary_risk"] = synthesis.get("primary_risk")
                record["what_market_misses"] = synthesis.get("what_market_misses")
                record["entry_note"] = synthesis.get("entry_note")
            except Exception as e:
                log.error("synthesis_failed",
                          ticker=record.get("ticker"), error=str(e)[:200])

    # Step 5: Batch upsert in chunks of 500
    if batch:
        chunk_size = 500
        for i in range(0, len(batch), chunk_size):
            chunk = batch[i:i + chunk_size]
            await db.table("india_hidden_gems").upsert(
                chunk, on_conflict="isin"
            ).execute()
            log.info("batch_upsert", chunk=i // chunk_size + 1,
                     records=len(chunk))

    log.info("composite_scoring_complete", **results)
    return results
