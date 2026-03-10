"""
promoter_scorer.py
Computes promoter_signal_score (0-100) for each company
from rolling 12-month insider trading history.

No Claude — pure Python signal computation.
Reads from: india_promoter_signals
Writes to:  india_promoter_summary

Signal logic:
  Open market buy      = strongest signal (+9 base per transaction)
  Warrant at premium   = private valuation signal (+7)
  Creeping acquisition = systematic accumulation (+7)
  Pledge reduction     = financial health improving (+6)
  Pledge increase      = stress signal (−8)
  Open market sell     = negative (−5)

Amplifiers applied for: size, clustering, pledge going to zero.
"""

import asyncio
import structlog
from datetime import datetime, timedelta, date
from typing import Optional

log = structlog.get_logger()

SIGNAL_WEIGHTS = {
    "open_market_buy":       9,
    "warrant_allotment":     7,
    "creeping_acquisition":  7,
    "pledge_decrease":       6,
    "preferential_allotment":3,
    "esop_exercise":         2,
    "off_market":            1,
    "open_market_sell":     -5,
    "pledge_increase":      -8,
}

# Maximum contribution per signal type (prevents single event dominating)
SIGNAL_CAP = {
    "open_market_buy":    25,
    "warrant_allotment":  20,
    "creeping_acquisition":20,
    "pledge_decrease":    15,
    "open_market_sell":  -20,
    "pledge_increase":   -20,
}


def compute_promoter_score(signals: list[dict]) -> dict:
    """
    Pure function — takes list of signals, returns score dict.
    Decoupled from DB for easy testing.
    """
    if not signals:
        return {
            "promoter_signal_score": 0,
            "score_narrative": "No insider trading disclosures in last 12 months",
            "highest_conviction_signal": None,
            "open_market_buying_cr_12m": 0,
            "open_market_selling_cr_12m": 0,
            "net_buying_cr_12m": 0,
            "buy_transaction_count_12m": 0,
            "warrant_issued_12m": False,
            "creeping_acq_active": False,
            "pledge_trend": "unknown",
        }

    # Aggregate metrics
    buy_signals = [s for s in signals if s.get("signal_type") == "open_market_buy"]
    sell_signals = [s for s in signals if s.get("signal_type") == "open_market_sell"]
    pledge_dec = [s for s in signals if s.get("signal_type") == "pledge_decrease"]
    pledge_inc = [s for s in signals if s.get("signal_type") == "pledge_increase"]

    buy_cr = sum(s.get("value_cr", 0) or 0 for s in buy_signals)
    sell_cr = sum(s.get("value_cr", 0) or 0 for s in sell_signals)
    buy_count = len(buy_signals)

    # Score computation
    type_totals: dict[str, float] = {}
    for s in signals:
        stype = s.get("signal_type", "other")
        weight = SIGNAL_WEIGHTS.get(stype, 0)
        if weight == 0:
            continue

        # Amplifiers
        amplifier = 1.0
        value_cr = s.get("value_cr", 0) or 0

        if stype == "open_market_buy":
            if value_cr >= 2:
                amplifier *= 1.3    # Meaningful size
            if value_cr >= 5:
                amplifier *= 1.2    # Large position
            if buy_count >= 5:
                amplifier *= 1.2    # Consistent accumulation pattern

        if stype == "pledge_decrease":
            after = s.get("pledge_pct_after") or s.get("post_transaction_pct", 100)
            if after is not None and float(after) < 2:
                amplifier *= 1.5    # Pledge going to zero = major catalyst

        contribution = weight * amplifier
        cap = SIGNAL_CAP.get(stype, 15)
        type_totals[stype] = max(
            min(type_totals.get(stype, 0) + contribution, abs(cap)),
            -abs(cap)
        ) if cap > 0 else max(
            min(type_totals.get(stype, 0) + contribution, -1),
            cap
        )

    raw_score = sum(type_totals.values())
    final_score = max(0, min(100, int(raw_score)))

    # Pledge trend
    if pledge_dec and not pledge_inc:
        pledge_trend = "falling"
    elif pledge_inc and not pledge_dec:
        pledge_trend = "rising"
    elif pledge_inc and pledge_dec:
        pledge_trend = "volatile"
    else:
        pledge_trend = "stable"

    # Narrative
    narrative = _build_narrative(
        final_score, buy_cr, sell_cr, buy_count, pledge_dec, pledge_inc
    )

    # Highest conviction signal
    highest = _highest_conviction(signals)

    return {
        "promoter_signal_score": final_score,
        "score_narrative": narrative,
        "highest_conviction_signal": highest,
        "open_market_buying_cr_12m": round(buy_cr, 2),
        "open_market_selling_cr_12m": round(sell_cr, 2),
        "net_buying_cr_12m": round(buy_cr - sell_cr, 2),
        "buy_transaction_count_12m": buy_count,
        "warrant_issued_12m": any(s.get("signal_type") == "warrant_allotment" for s in signals),
        "creeping_acq_active": any(s.get("signal_type") == "creeping_acquisition" for s in signals),
        "pledge_trend": pledge_trend,
    }


def _build_narrative(score, buy_cr, sell_cr, buy_count, pledge_dec, pledge_inc) -> str:
    parts = []

    if buy_cr > 0:
        if buy_count == 1:
            parts.append(f"Promoter bought ₹{buy_cr:.1f} Cr on open market")
        else:
            parts.append(f"Promoter bought ₹{buy_cr:.1f} Cr across {buy_count} transactions")

    if sell_cr > buy_cr and sell_cr > 0:
        parts.append(f"⚠️ Selling ₹{sell_cr:.1f} Cr exceeds buying")

    if pledge_dec:
        latest = pledge_dec[0]
        before = latest.get("pledged_shares_before", latest.get("post_transaction_pct", "?"))
        after = latest.get("pledged_shares_after", latest.get("pledge_pct_after", "?"))
        if before != "?" and after != "?":
            parts.append(f"Pledge reduced: {before}% → {after}%")
        else:
            parts.append("Pledge reduction filed")

    if pledge_inc:
        parts.append(f"⚠️ Pledge creation in last 12 months")

    if not parts:
        if score > 0:
            parts.append("Minor positive signals detected")
        else:
            parts.append("No significant promoter activity")

    suffix = ""
    if score >= 70:
        suffix = " → STRONG alignment with minority shareholders"
    elif score >= 50:
        suffix = " → Net positive insider activity"
    elif score <= 15:
        suffix = " → CAUTION: adverse signals"

    return ". ".join(parts) + suffix


def _highest_conviction(signals: list[dict]) -> Optional[str]:
    """Return the single most informative signal."""
    # Priority order
    for stype in ["open_market_buy", "creeping_acquisition",
                  "warrant_allotment", "pledge_decrease"]:
        matching = [s for s in signals if s.get("signal_type") == stype]
        if matching:
            s = matching[0]
            v = s.get("value_cr", 0) or 0
            dt = s.get("transaction_date", "")
            if stype == "open_market_buy":
                return f"Open market buy ₹{v:.1f} Cr on {dt}"
            elif stype == "creeping_acquisition":
                return f"Creeping acquisition — systematic accumulation as of {dt}"
            elif stype == "warrant_allotment":
                return f"Warrants issued at ₹{s.get('price_per_share',0):.0f} on {dt}"
            elif stype == "pledge_decrease":
                after = s.get("pledge_pct_after", "?")
                return f"Pledge reduced to {after}% as of {dt}"
    return None


async def score_all_companies(db) -> dict:
    """
    Score promoter signals for ALL companies in india_companies
    that have at least one signal in last 12 months.
    """
    results = {"scored": 0, "no_signals": 0, "errors": 0}
    cutoff = (datetime.now() - timedelta(days=365)).date().isoformat()

    # Get all unique ISINs that have signals (use paginated fetch for full coverage)
    try:
        from india_alpha.db import fetch_all_rows
        all_signals = await fetch_all_rows(
            db, "india_promoter_signals",
            select="isin, ticker",
            gte={"transaction_date": cutoff},
        )
    except Exception as e:
        log.error("fetch_signal_isins_failed", error=str(e))
        return results

    # Unique ISINs
    seen = set()
    companies = []
    for row in all_signals:
        if row["isin"] not in seen:
            seen.add(row["isin"])
            companies.append({"isin": row["isin"], "ticker": row["ticker"]})

    log.info("scoring_promoter_signals", companies=len(companies))

    for company in companies:
        try:
            await score_one_company(db, company["isin"], company["ticker"])
            results["scored"] += 1
        except Exception as e:
            log.error("score_company_failed",
                      isin=company["isin"], error=str(e)[:100])
            results["errors"] += 1

    log.info("promoter_scoring_complete", **results)
    return results


async def score_one_company(db, isin: str, ticker: str) -> dict:
    """Score one company and upsert into india_promoter_summary."""
    cutoff = (datetime.now() - timedelta(days=365)).date().isoformat()

    signals_result = await db.table("india_promoter_signals") \
        .select("*") \
        .eq("isin", isin) \
        .gte("transaction_date", cutoff) \
        .order("transaction_date", desc=True) \
        .execute()

    signals = signals_result.data or []
    score_data = compute_promoter_score(signals)

    record = {
        "isin": isin,
        "ticker": ticker,
        **score_data,
        "updated_at": datetime.now().isoformat(),
    }

    await db.table("india_promoter_summary").upsert(
        record, on_conflict="isin"
    ).execute()

    log.debug("promoter_scored",
              ticker=ticker,
              score=score_data["promoter_signal_score"])
    return score_data
