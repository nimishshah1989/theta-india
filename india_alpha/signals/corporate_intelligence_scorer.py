"""
corporate_intelligence_scorer.py
Scores companies based on NSE corporate filings — replaces concall_scorer.py.

Two scoring modes:
  A) Python rules (free) — for most filings: credit ratings, dividends,
     management changes, auditor changes, acquisitions, press releases.
  B) Claude analysis (cost-controlled) — for top HIGH-priority filings
     with extracted PDF text (concall transcripts, board outcomes, investor decks).

Aggregation:
  Per-bucket scores (earnings_strategy 55%, capital_action 30%, governance 15%)
  weighted by recency, combined into one corporate_intelligence_score (0-100).

Cost: ~$0.01-0.02 per Claude call. Max 3 per company, 40 per pipeline run.
"""

import asyncio
import json
import re
from datetime import date, datetime
from typing import Optional

import anthropic
import structlog

from india_alpha.cost_tracker import CostTracker

log = structlog.get_logger()

# Bucket weights for final aggregation
BUCKET_WEIGHTS = {
    "earnings_strategy": 0.55,
    "capital_action": 0.30,
    "governance": 0.15,
}

# Recency multipliers — more recent filings matter more
RECENCY_MULTIPLIERS = {
    90: 1.0,    # Last 3 months: full weight
    180: 0.8,   # 3-6 months: 80%
    365: 0.5,   # 6-12 months: 50%
    999: 0.3,   # Older: 30%
}

MAX_CLAUDE_CALLS_PER_COMPANY = 3
MAX_CLAUDE_CALLS_PER_RUN = 40

# ──────────────────────────────────────────────────────────────────
# PYTHON RULES SCORING (free — no API cost)
# ──────────────────────────────────────────────────────────────────

def _score_credit_rating(subject: str) -> tuple[int, str]:
    """Score credit rating filings from subject text."""
    lower = subject.lower()

    if any(w in lower for w in ["upgrade", "revised upward", "rating improved"]):
        return (15, "Credit rating upgrade")
    elif any(w in lower for w in ["reaffirm", "maintain", "ratified"]):
        return (5, "Credit rating reaffirmed")
    elif any(w in lower for w in ["downgrade", "revised downward", "rating reduced"]):
        return (-10, "Credit rating downgrade")
    elif any(w in lower for w in ["watch", "negative outlook"]):
        return (-5, "Credit rating on watch/negative outlook")

    return (3, "Credit rating update")


def _score_dividend(subject: str) -> tuple[int, str]:
    """Score dividend announcements."""
    lower = subject.lower()

    if any(w in lower for w in ["special dividend", "interim dividend"]):
        return (10, "Special/interim dividend announced")
    elif "final dividend" in lower:
        return (5, "Final dividend declared")

    return (5, "Dividend declared")


def _score_management_change(subject: str) -> tuple[int, str]:
    """Score appointment/cessation of key management."""
    lower = subject.lower()

    # Resignations of key people are concerning
    if "resignation" in lower or "cessation" in lower:
        if any(w in lower for w in ["cfo", "chief financial", "ceo", "managing director", "md"]):
            return (-8, "CFO/CEO/MD resignation")
        elif any(w in lower for w in ["director", "whole time", "executive"]):
            return (-4, "Executive director cessation")
        return (-2, "Management cessation")

    # Appointments are mildly positive
    if "appointment" in lower:
        if any(w in lower for w in ["cfo", "chief financial", "ceo", "managing director"]):
            return (5, "Key management appointment (CFO/CEO/MD)")
        return (3, "KMP appointment")

    return (0, "Management change")


def _score_auditor_change(subject: str) -> tuple[int, str]:
    """Score auditor changes — mid-term change is a red flag."""
    lower = subject.lower()

    if any(w in lower for w in ["resignation of auditor", "removal of auditor",
                                  "casual vacancy", "mid-term"]):
        return (-12, "Mid-term auditor change (red flag)")
    elif "rotation" in lower or "re-appointment" in lower:
        return (-2, "Auditor rotation (routine)")

    return (-5, "Auditor change")


def _score_acquisition(subject: str) -> tuple[int, str]:
    """Score acquisition-related filings."""
    lower = subject.lower()

    if any(w in lower for w in ["strategic", "100%", "majority stake"]):
        return (12, "Strategic acquisition")
    elif any(w in lower for w in ["subsidiary", "joint venture", "jv"]):
        return (8, "Subsidiary/JV formation")
    elif any(w in lower for w in ["stake", "shares acquired", "equity interest"]):
        return (8, "Stake acquisition")

    return (6, "Acquisition activity")


def _score_press_release(subject: str) -> tuple[int, str]:
    """Score press releases by content type."""
    lower = subject.lower()

    if any(w in lower for w in ["order win", "order received", "order book",
                                  "contract awarded", "new order"]):
        return (10, "Order win/contract award")
    elif any(w in lower for w in ["partnership", "collaboration", "alliance",
                                    "agreement", "mou"]):
        return (8, "Partnership/collaboration announcement")
    elif any(w in lower for w in ["expansion", "new plant", "capacity",
                                    "greenfield", "capex"]):
        return (8, "Capacity expansion announcement")
    elif any(w in lower for w in ["patent", "innovation", "r&d", "technology"]):
        return (6, "Innovation/technology milestone")
    elif any(w in lower for w in ["litigation", "penalty", "show cause", "dispute"]):
        return (-3, "Litigation/regulatory concern")
    elif any(w in lower for w in ["loss", "fire", "accident", "shutdown"]):
        return (-5, "Adverse event reported")

    return (3, "Press release")


def _score_sebi_takeover(subject: str) -> tuple[int, str]:
    """Score SEBI takeover regulation disclosures."""
    lower = subject.lower()

    if any(w in lower for w in ["acquisition of shares", "increase in holding"]):
        return (8, "Stake increase (takeover disclosure)")
    elif any(w in lower for w in ["disposal", "decrease", "sale of shares"]):
        return (-5, "Stake decrease (disposal)")

    return (3, "SEBI takeover disclosure")


def _score_buyback(subject: str) -> tuple[int, str]:
    """Score buyback-related filings."""
    return (10, "Buyback announced — management sees undervaluation")


def _score_bonus_split(subject: str) -> tuple[int, str]:
    """Score bonus/split filings."""
    lower = subject.lower()
    if "bonus" in lower:
        return (5, "Bonus issue — positive signal")
    return (3, "Stock split")


def _score_esop(subject: str) -> tuple[int, str]:
    """Score ESOP-related filings."""
    return (2, "ESOP/ESOS activity")


def score_filing_python(filing: dict) -> tuple[int, str]:
    """
    Score a single filing using Python rules (no API cost).
    Returns (score_delta, reason).
    """
    category = filing.get("category", "")
    subject = filing.get("subject_text", "") or ""
    bucket = filing.get("category_bucket", "")

    scoring_map = {
        "credit rating": _score_credit_rating,
        "dividend": _score_dividend,
        "appointment": _score_management_change,
        "cessation": _score_management_change,
        "resignation": _score_management_change,
        "change in director": _score_management_change,
        "auditor": _score_auditor_change,
        "change in auditor": _score_auditor_change,
        "acquisition": _score_acquisition,
        "amalgamation": _score_acquisition,
        "merger": _score_acquisition,
        "press release": _score_press_release,
        "takeover": _score_sebi_takeover,
        "regulation 29": _score_sebi_takeover,
        "buyback": _score_buyback,
        "bonus": _score_bonus_split,
        "split": _score_bonus_split,
        "esop": _score_esop,
        "esos": _score_esop,
    }

    scorer = scoring_map.get(category)
    if scorer:
        return scorer(subject)

    # Default small score for non-category filings
    return (1, "Filing recorded")


# ──────────────────────────────────────────────────────────────────
# CLAUDE ANALYSIS (cost-controlled — for HIGH priority PDFs only)
# ──────────────────────────────────────────────────────────────────

EARNINGS_ANALYSIS_PROMPT = """\
You are a forensic equity analyst specialising in Indian mid/small-cap companies.
Analyse this corporate filing and extract investment-relevant signals.

Company: {company_name} ({ticker})
Filing Type: {filing_type}
Date: {filing_date}
Word count: {word_count}

═══ FILING TEXT (may be truncated) ═══
{text}
═══ END ═══

TASK: Score this filing on 5 dimensions. Return ONLY valid JSON:

{{
  "management_tone": "bullish|confident|neutral|cautious|defensive",
  "tone_score": <0-25>,
  "tone_reasoning": "<1 sentence — what specific language/data drove this rating>",
  "forward_signals": [
    {{"signal": "<specific forward-looking statement>", "strength": "strong|moderate|weak"}}
  ],
  "forward_score": <0-25>,
  "quantitative_commitments": [
    {{"metric": "<revenue/margin/capex/etc>", "target": "<specific number or range>", "timeline": "<when>"}}
  ],
  "quant_score": <0-25>,
  "red_flags": [
    {{"flag": "<specific concern>", "severity": "high|medium|low"}}
  ],
  "red_flag_deduction": <0 to -15>,
  "hidden_insight": "<1-2 sentences — what would most analysts miss>",
  "hidden_insight_score": <0-10>,
  "investability_signal": "strong_buy_signal|positive|neutral|negative|red_flag"
}}

Score conservatively — a neutral filing should score 30-40 total.
"""

CAPITAL_ACTION_PROMPT = """\
You are a forensic equity analyst specialising in Indian mid/small-cap companies.
Analyse this corporate action filing for investment signals.

Company: {company_name} ({ticker})
Filing Type: {filing_type}
Date: {filing_date}
Word count: {word_count}

═══ FILING TEXT (may be truncated) ═══
{text}
═══ END ═══

TASK: Assess this capital action for strategic value. Return ONLY valid JSON:

{{
  "action_type": "<acquisition|expansion|restructuring|divestment|other>",
  "strategic_value": "high|medium|low",
  "strategic_score": <0-25>,
  "financial_impact": "<1 sentence — quantify the impact if possible>",
  "financial_score": <0-25>,
  "execution_risk": "low|medium|high",
  "execution_score": <0-25>,
  "red_flags": [
    {{"flag": "<specific concern>", "severity": "high|medium|low"}}
  ],
  "red_flag_deduction": <0 to -15>,
  "hidden_insight": "<1-2 sentences — what would most analysts miss>",
  "hidden_insight_score": <0-10>,
  "investability_signal": "strong_buy_signal|positive|neutral|negative|red_flag"
}}

Focus on whether this action creates long-term value or signals management quality.
"""


def _clean_and_parse_json(text: str) -> dict:
    """
    Robustly parse JSON from Claude's response.
    Handles common issues: trailing commas, unescaped quotes in strings, markdown wrapping.
    """
    # Strip markdown code fences if present
    cleaned = re.sub(r'^```(?:json)?\s*', '', text.strip())
    cleaned = re.sub(r'\s*```$', '', cleaned)

    # Try direct parse first
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Extract JSON object from surrounding text
    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if not match:
        raise json.JSONDecodeError("No JSON object found", text, 0)

    json_str = match.group()

    # Remove trailing commas before } or ]
    json_str = re.sub(r',\s*([}\]])', r'\1', json_str)

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    # Last resort: try to fix unescaped quotes inside string values
    # Replace inner quotes with escaped quotes (common Claude issue)
    json_str = re.sub(
        r'(?<=: ")(.*?)(?="[,\s\n}])',
        lambda m: m.group(0).replace('"', '\\"') if m.group(0).count('"') > 0 else m.group(0),
        json_str,
    )

    return json.loads(json_str)


def _truncate_text(text: str, max_chars: int = 12000) -> str:
    """Truncate text for Claude context, keeping start + end."""
    if not text or len(text) <= max_chars:
        return text or ""

    first_portion = int(max_chars * 0.4)
    last_portion = max_chars - first_portion

    return (
        text[:first_portion]
        + "\n\n[... text truncated for analysis ...]\n\n"
        + text[-last_portion:]
    )


def _compute_claude_score(analysis: dict) -> int:
    """Compute total score from Claude's analysis components."""
    # For earnings_strategy prompt format
    tone = min(25, max(0, analysis.get("tone_score", 0)))
    forward = min(25, max(0, analysis.get("forward_score", 0)))
    quant = min(25, max(0, analysis.get("quant_score", 0)))

    # For capital_action prompt format
    strategic = min(25, max(0, analysis.get("strategic_score", 0)))
    financial = min(25, max(0, analysis.get("financial_score", 0)))
    execution = min(25, max(0, analysis.get("execution_score", 0)))

    red_flag = max(-15, min(0, analysis.get("red_flag_deduction", 0)))
    hidden = min(10, max(0, analysis.get("hidden_insight_score", 0)))

    # Use whichever prompt format was used
    if strategic > 0 or financial > 0 or execution > 0:
        total = strategic + financial + execution + red_flag + hidden
    else:
        total = tone + forward + quant + red_flag + hidden

    return max(0, min(100, total))


async def analyse_filing_with_claude(
    claude_client: anthropic.AsyncAnthropic,
    filing: dict,
    model: str = "claude-sonnet-4-6",
    cost_tracker: Optional[CostTracker] = None,
) -> dict:
    """
    Use Claude to analyse a single filing with extracted text.
    Returns analysis dict with scores and signals.
    """
    # Budget check
    if cost_tracker and not cost_tracker.can_call():
        log.warning("claude_budget_exhausted",
                    ticker=filing.get("ticker"),
                    spend=cost_tracker.estimated_spend_usd)
        return _empty_claude_analysis("Daily Claude budget exhausted")

    text = _truncate_text(filing.get("extracted_text", ""))

    if not text or len(text) < 200:
        return _empty_claude_analysis("Filing text too short for analysis")

    bucket = filing.get("category_bucket", "earnings_strategy")
    prompt_template = (CAPITAL_ACTION_PROMPT
                       if bucket == "capital_action"
                       else EARNINGS_ANALYSIS_PROMPT)

    prompt = prompt_template.format(
        company_name=filing.get("company_name", "Unknown"),
        ticker=filing.get("ticker", ""),
        filing_type=filing.get("category", ""),
        filing_date=filing.get("sort_date", ""),
        word_count=filing.get("word_count", 0),
        text=text,
    )

    try:
        response = await claude_client.messages.create(
            model=model,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        response_text = response.content[0].text

        # Track cost using actual token counts
        if cost_tracker:
            cost_tracker.record_call(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

        try:
            analysis = _clean_and_parse_json(response_text)
        except (json.JSONDecodeError, Exception) as parse_err:
            log.warning("claude_json_parse_failed",
                        ticker=filing.get("ticker"),
                        error=str(parse_err)[:80])
            return _empty_claude_analysis("Failed to parse Claude response")

        analysis["claude_score"] = _compute_claude_score(analysis)
        return analysis

    except anthropic.APIError as exc:
        log.error("claude_api_error",
                  ticker=filing.get("ticker"), error=str(exc)[:100])
        return _empty_claude_analysis(f"API error: {str(exc)[:80]}")
    except Exception as exc:
        log.error("claude_filing_analysis_failed",
                  ticker=filing.get("ticker"), error=str(exc)[:100])
        return _empty_claude_analysis(f"Analysis error: {str(exc)[:80]}")


def _empty_claude_analysis(reason: str = "No analysis") -> dict:
    """Return empty analysis structure."""
    return {
        "management_tone": "neutral",
        "tone_score": 0,
        "tone_reasoning": reason,
        "forward_signals": [],
        "forward_score": 0,
        "quantitative_commitments": [],
        "quant_score": 0,
        "red_flags": [],
        "red_flag_deduction": 0,
        "hidden_insight": None,
        "hidden_insight_score": 0,
        "investability_signal": "neutral",
        "claude_score": 0,
    }


# ──────────────────────────────────────────────────────────────────
# AGGREGATION — Per-company score from all filings
# ──────────────────────────────────────────────────────────────────

def _get_recency_multiplier(filing_date_str: str) -> float:
    """Get recency weight based on filing age."""
    try:
        if isinstance(filing_date_str, str):
            filing_date = date.fromisoformat(filing_date_str[:10])
        else:
            filing_date = filing_date_str
        days_ago = (date.today() - filing_date).days
    except (ValueError, TypeError):
        days_ago = 999

    for threshold, multiplier in sorted(RECENCY_MULTIPLIERS.items()):
        if days_ago <= threshold:
            return multiplier
    return 0.3


def aggregate_company_score(
    python_scores: list[dict],
    claude_analyses: list[dict],
) -> dict:
    """
    Aggregate all filing scores into one corporate_intelligence_score.

    python_scores: [{"bucket": str, "score": int, "reason": str, "sort_date": str}, ...]
    claude_analyses: [{"bucket": str, "claude_score": int, "analysis": dict}, ...]

    Returns: {"corporate_intelligence_score": int, "earnings_strategy_score": int, ...}
    """
    bucket_scores = {
        "earnings_strategy": [],
        "capital_action": [],
        "governance": [],
    }

    # Add Python-scored filings
    for ps in python_scores:
        bucket = ps.get("bucket", "governance")
        if bucket not in bucket_scores:
            bucket_scores[bucket] = []

        recency = _get_recency_multiplier(ps.get("sort_date", ""))
        weighted_score = ps["score"] * recency
        bucket_scores[bucket].append(weighted_score)

    # Add Claude-scored filings (weighted higher — they have richer data)
    for ca in claude_analyses:
        bucket = ca.get("bucket", "earnings_strategy")
        if bucket not in bucket_scores:
            bucket_scores[bucket] = []

        recency = _get_recency_multiplier(ca.get("sort_date", ""))
        # Claude scores are on 0-100 scale, normalize to comparable range
        weighted_score = (ca.get("claude_score", 0) / 100) * 25 * recency
        bucket_scores[bucket].append(weighted_score)

    # Compute per-bucket scores
    earnings_raw = sum(bucket_scores["earnings_strategy"])
    capital_raw = sum(bucket_scores["capital_action"])
    governance_raw = sum(bucket_scores["governance"])

    # Normalize each bucket to 0-100
    earnings_score = min(100, max(0, int(earnings_raw)))
    capital_score = min(100, max(0, int(capital_raw)))
    governance_score = min(100, max(0, int(governance_raw)))

    # Weighted composite
    composite = (
        earnings_score * BUCKET_WEIGHTS["earnings_strategy"]
        + capital_score * BUCKET_WEIGHTS["capital_action"]
        + governance_score * BUCKET_WEIGHTS["governance"]
    )

    return {
        "corporate_intelligence_score": min(100, max(0, int(composite))),
        "earnings_strategy_score": earnings_score,
        "capital_action_score": capital_score,
        "governance_score": governance_score,
    }


# ──────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ──────────────────────────────────────────────────────────────────

async def score_company_filings(
    db,
    isin: str,
    ticker: str,
    claude_client: Optional[anthropic.AsyncAnthropic] = None,
    model: str = "claude-sonnet-4-6",
    cost_tracker: Optional[CostTracker] = None,
) -> dict:
    """
    Score all filings for one company. Main per-company entry point.
    Returns the aggregated score record.
    """
    # Get all unanalysed filings for this company
    filings_result = await db.table("india_corporate_filings") \
        .select("*") \
        .eq("isin", isin) \
        .eq("is_analysed", False) \
        .order("sort_date", desc=True) \
        .execute()

    filings = filings_result.data or []

    # Also get already-analysed filings for full picture
    analysed_result = await db.table("india_corporate_filings") \
        .select("*") \
        .eq("isin", isin) \
        .eq("is_analysed", True) \
        .order("sort_date", desc=True) \
        .execute()

    all_filings = filings + (analysed_result.data or [])

    if not all_filings:
        return {"corporate_intelligence_score": 0, "filings_analysed": 0}

    python_scores = []
    claude_analyses = []
    forward_signals = []
    capital_actions = []
    governance_flags = []
    management_tone = "neutral"
    hidden_insight = None
    claude_calls_made = 0

    # Step 1: Python-score ALL filings (free)
    for filing in all_filings:
        score, reason = score_filing_python(filing)
        python_scores.append({
            "bucket": filing.get("category_bucket", "governance"),
            "score": score,
            "reason": reason,
            "sort_date": filing.get("sort_date", ""),
        })

        # Collect notable items for the summary
        if score >= 8:
            bucket = filing.get("category_bucket", "")
            if bucket == "capital_action":
                capital_actions.append(reason)
            elif bucket == "governance":
                governance_flags.append(reason)
            else:
                forward_signals.append(reason)
        elif score <= -5:
            governance_flags.append(reason)

    # Step 2: Claude-analyse top HIGH-priority filings with text (cost-controlled)
    if claude_client and (not cost_tracker or cost_tracker.can_call()):
        # Select candidates: HIGH priority, has extracted text, unanalysed
        claude_candidates = [
            f for f in filings
            if f.get("signal_priority") == "HIGH"
            and f.get("is_text_extracted")
            and (f.get("word_count") or 0) >= 200
        ]

        # Limit to top N by recency
        claude_candidates = claude_candidates[:MAX_CLAUDE_CALLS_PER_COMPANY]

        for filing in claude_candidates:
            # Check budget before each call
            if cost_tracker and not cost_tracker.can_call():
                log.info("claude_budget_reached", ticker=ticker)
                break

            analysis = await analyse_filing_with_claude(
                claude_client, filing, model, cost_tracker
            )
            claude_calls_made += 1

            claude_analyses.append({
                "bucket": filing.get("category_bucket", "earnings_strategy"),
                "claude_score": analysis.get("claude_score", 0),
                "sort_date": filing.get("sort_date", ""),
                "analysis": analysis,
            })

            # Extract key intelligence
            if analysis.get("management_tone") and analysis["management_tone"] != "neutral":
                management_tone = analysis["management_tone"]

            if analysis.get("hidden_insight"):
                hidden_insight = analysis["hidden_insight"]

            for sig in analysis.get("forward_signals", []):
                if sig.get("strength") in ("strong", "moderate"):
                    forward_signals.append(sig.get("signal", ""))

            for flag in analysis.get("red_flags", []):
                if flag.get("severity") in ("high", "medium"):
                    governance_flags.append(flag.get("flag", ""))

            # Mark filing as analysed
            await db.table("india_corporate_filings").update({
                "is_analysed": True,
                "analysed_at": datetime.now().isoformat(),
            }).eq("id", filing["id"]).execute()

            await asyncio.sleep(0.5)

    # Batch-mark all unanalysed filings as analysed
    unanalysed_ids = [f["id"] for f in filings if not f.get("is_analysed")]
    if unanalysed_ids:
        await db.table("india_corporate_filings").update({
            "is_analysed": True,
            "analysed_at": datetime.now().isoformat(),
        }).in_("id", unanalysed_ids).execute()

    # Step 3: Aggregate
    aggregated = aggregate_company_score(python_scores, claude_analyses)

    # Build score narrative
    narrative_parts = []
    if aggregated["earnings_strategy_score"] > 30:
        narrative_parts.append(f"Strong earnings signals ({aggregated['earnings_strategy_score']})")
    if aggregated["capital_action_score"] > 20:
        narrative_parts.append(f"Active capital actions ({aggregated['capital_action_score']})")
    if aggregated["governance_score"] < -5:
        narrative_parts.append(f"Governance concerns ({aggregated['governance_score']})")

    score_narrative = "; ".join(narrative_parts) if narrative_parts else "Moderate corporate activity"

    # Get latest filing date
    latest_date = None
    for filing in all_filings:
        sd = filing.get("sort_date")
        if sd:
            if latest_date is None or sd > latest_date:
                latest_date = sd

    record = {
        "isin": isin,
        "ticker": ticker,
        "corporate_intelligence_score": aggregated["corporate_intelligence_score"],
        "earnings_strategy_score": aggregated["earnings_strategy_score"],
        "capital_action_score": aggregated["capital_action_score"],
        "governance_score": aggregated["governance_score"],
        "management_tone": management_tone,
        "key_forward_signals": forward_signals[:5],
        "key_capital_actions": capital_actions[:5],
        "governance_flags": governance_flags[:5],
        "hidden_insight": hidden_insight,
        "filings_analysed": len(all_filings),
        "filings_available": len(all_filings),
        "latest_filing_date": latest_date,
        "score_narrative": score_narrative,
        "scored_at": datetime.now().isoformat(),
    }

    # Upsert into scores table
    await db.table("india_corporate_intelligence_scores").upsert(
        record, on_conflict="isin"
    ).execute()

    log.info("corporate_intelligence_scored",
             ticker=ticker,
             score=aggregated["corporate_intelligence_score"],
             filings=len(all_filings),
             claude_calls=claude_calls_made,
             tone=management_tone)

    return {**record, "claude_calls": claude_calls_made}


async def score_all_companies(
    db,
    claude_client: Optional[anthropic.AsyncAnthropic] = None,
    model: str = "claude-sonnet-4-6",
    cost_tracker: Optional[CostTracker] = None,
) -> dict:
    """
    Score all companies that have unprocessed corporate filings.
    Main pipeline entry point for Step 9.
    """
    results = {"scored": 0, "claude_calls": 0, "errors": 0}

    # Get companies with unanalysed filings
    from india_alpha.db import fetch_all_rows
    filings_rows = await fetch_all_rows(
        db, "india_corporate_filings", select="isin, ticker",
        eq={"is_analysed": False}
    )

    # Deduplicate by ISIN
    seen = set()
    companies = []
    for row in filings_rows:
        if row["isin"] not in seen:
            seen.add(row["isin"])
            companies.append({"isin": row["isin"], "ticker": row["ticker"]})

    log.info("corporate_intelligence_scoring_start", companies=len(companies))

    for company in companies:
        try:
            result = await score_company_filings(
                db, company["isin"], company["ticker"],
                claude_client, model, cost_tracker,
            )
            results["scored"] += 1
            results["claude_calls"] += result.get("claude_calls", 0)

        except Exception as exc:
            log.error("company_scoring_failed",
                      ticker=company.get("ticker"),
                      error=str(exc)[:100])
            results["errors"] += 1

    log.info("corporate_intelligence_scoring_complete", **results)
    return results
