"""
DEPRECATED — Use corporate_intelligence_scorer.py instead.

concall_scorer.py
Analyses earnings call transcripts using Claude (transcript-only).
Replaced by corporate_intelligence_scorer.py which scores 12 filing categories
using Python rules (free) + Claude for top filings only.
Kept for backward compatibility with existing india_concall_signals table data.

Five signal dimensions (scored 0-100):
  1. Management Tone (0-25): Bullish/Confident/Neutral/Cautious/Defensive
  2. Forward Signals (0-25): Guidance, expansion plans, order book mentions
  3. Quantitative Commitments (0-25): Revenue/margin targets, capex, timeline specificity
  4. Red Flags (0 to -15): Deferrals, vague answers, auditor concerns
  5. Hidden Insight (0-10): What most analysts would miss

Cost control: Only analyses companies already scoring >= 25 in promoter OR OL.
~$0.01-0.02 per transcript. Budget: ~$2/quarter for 50 transcripts.
"""

import json
import asyncio
import structlog
from datetime import datetime
from typing import Optional
import anthropic

log = structlog.get_logger()


CONCALL_ANALYSIS_PROMPT = """\
You are a forensic equity analyst specialising in Indian mid/small-cap companies.
Analyse this earnings call transcript and extract investment-relevant signals.

Company: {company_name} ({ticker})
Quarter: {quarter}
Transcript length: {word_count} words

═══ TRANSCRIPT (may be truncated) ═══
{transcript}
═══ END TRANSCRIPT ═══

TASK: Score this concall on 5 dimensions. Return ONLY valid JSON:

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
  "hidden_insight": "<1-2 sentences — what would most analysts miss in this transcript>",
  "hidden_insight_score": <0-10>,
  "investability_signal": "strong_buy_signal|positive|neutral|negative|red_flag"
}}

Be precise. Score conservatively — a neutral concall should score 30-40 total.
Only flag red_flags for genuinely concerning patterns, not minor issues.
"""


def _truncate_transcript(text: str, max_chars: int = 12000) -> str:
    """
    Truncate transcript to fit context window while preserving key sections.
    Keep opening remarks + Q&A section (most informative).
    """
    if not text or len(text) <= max_chars:
        return text or ""

    # Take first 40% (management commentary) + last 60% (Q&A)
    first_portion = int(max_chars * 0.4)
    last_portion = max_chars - first_portion

    return (
        text[:first_portion]
        + "\n\n[... transcript truncated for analysis ...]\n\n"
        + text[-last_portion:]
    )


def _compute_total_score(analysis: dict) -> int:
    """Compute total concall score from Claude's analysis components."""
    tone = min(25, max(0, analysis.get("tone_score", 0)))
    forward = min(25, max(0, analysis.get("forward_score", 0)))
    quant = min(25, max(0, analysis.get("quant_score", 0)))
    red_flag = max(-15, min(0, analysis.get("red_flag_deduction", 0)))
    hidden = min(10, max(0, analysis.get("hidden_insight_score", 0)))

    total = tone + forward + quant + red_flag + hidden
    return max(0, min(100, total))


async def analyse_concall(
    claude_client: anthropic.Anthropic,
    concall: dict,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """
    Use Claude to analyse a single concall transcript.
    Returns analysis dict with scores and signals.
    """
    transcript = _truncate_transcript(concall.get("transcript_text", ""))

    if not transcript or len(transcript) < 200:
        return _empty_analysis("Transcript too short for meaningful analysis")

    prompt = CONCALL_ANALYSIS_PROMPT.format(
        company_name=concall.get("company_name", "Unknown"),
        ticker=concall.get("ticker", ""),
        quarter=concall.get("quarter", ""),
        word_count=concall.get("word_count", 0),
        transcript=transcript,
    )

    try:
        response = claude_client.messages.create(
            model=model,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text

        # Parse JSON — with fallback
        try:
            analysis = json.loads(text)
        except json.JSONDecodeError:
            import re
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                analysis = json.loads(match.group())
            else:
                return _empty_analysis("Failed to parse Claude response")

        # Compute total score
        analysis["concall_signal_score"] = _compute_total_score(analysis)
        return analysis

    except Exception as e:
        log.error("concall_analysis_failed",
                  ticker=concall.get("ticker"), error=str(e)[:100])
        return _empty_analysis(f"Analysis error: {str(e)[:80]}")


def _empty_analysis(reason: str = "No analysis") -> dict:
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
        "concall_signal_score": 0,
    }


async def score_concall_and_store(
    db,
    concall: dict,
    claude_client: anthropic.Anthropic,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """Analyse one concall and store results in india_concall_signals."""
    analysis = await analyse_concall(claude_client, concall, model)

    record = {
        "concall_id": concall.get("id"),
        "isin": concall["isin"],
        "ticker": concall["ticker"],
        "quarter": concall["quarter"],
        "management_tone": analysis.get("management_tone"),
        "tone_reasoning": analysis.get("tone_reasoning"),
        "investability_signal": analysis.get("investability_signal"),
        "forward_signals": analysis.get("forward_signals", []),
        "quantitative_commitments": analysis.get("quantitative_commitments", []),
        "red_flags": analysis.get("red_flags", []),
        "order_book_cr": None,  # Extracted from quant commitments if available
        "hidden_insight": analysis.get("hidden_insight"),
        "concall_signal_score": analysis.get("concall_signal_score", 0),
        "score_reasoning": analysis.get("tone_reasoning"),
        "processed_at": datetime.now().isoformat(),
    }

    # Extract order book from quantitative commitments if mentioned
    for commitment in analysis.get("quantitative_commitments", []):
        metric = (commitment.get("metric") or "").lower()
        if "order book" in metric or "order_book" in metric:
            try:
                target = commitment.get("target", "")
                # Simple extraction of number from string
                import re
                nums = re.findall(r'[\d,]+\.?\d*', str(target))
                if nums:
                    record["order_book_cr"] = float(nums[0].replace(",", ""))
            except (ValueError, IndexError):
                pass

    # Upsert signal record
    await db.table("india_concall_signals").upsert(
        record, on_conflict="concall_id"
    ).execute()

    # Mark concall as processed
    if concall.get("id"):
        await db.table("india_concalls").update({
            "is_processed": True,
            "processed_at": datetime.now().isoformat(),
        }).eq("id", concall["id"]).execute()

    log.info("concall_analysed",
             ticker=concall["ticker"],
             quarter=concall["quarter"],
             score=analysis.get("concall_signal_score", 0),
             tone=analysis.get("management_tone"))

    return analysis


async def score_all_unprocessed_concalls(
    db,
    claude_client: anthropic.Anthropic,
    model: str = "claude-sonnet-4-6",
    max_transcripts: int = 50,
) -> dict:
    """
    Analyse all unprocessed concalls in the database.
    Only processes transcripts for companies with existing signal scores.
    """
    results = {"analysed": 0, "skipped": 0, "errors": 0, "claude_calls": 0}

    if not claude_client:
        log.warning("concall_scoring_skipped", msg="No Claude client configured")
        return {**results, "skipped": True, "reason": "no_claude_client"}

    # Get unprocessed concalls
    concalls_result = await db.table("india_concalls") \
        .select("*") \
        .eq("is_processed", False) \
        .order("fetched_at", desc=True) \
        .limit(max_transcripts) \
        .execute()

    concalls = concalls_result.data or []
    log.info("concall_scoring_start", unprocessed=len(concalls))

    for concall in concalls:
        try:
            # Skip if transcript is too short
            if (concall.get("word_count") or 0) < 100:
                results["skipped"] += 1
                continue

            await score_concall_and_store(db, concall, claude_client, model)
            results["analysed"] += 1
            results["claude_calls"] += 1

            # Respect rate limits
            await asyncio.sleep(0.5)

        except Exception as e:
            log.error("concall_score_failed",
                      ticker=concall.get("ticker"),
                      quarter=concall.get("quarter"),
                      error=str(e)[:100])
            results["errors"] += 1

    log.info("concall_scoring_complete", **results)
    return results
