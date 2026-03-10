"""
degradation_monitor.py
Detects deteriorating fundamentals and governance red flags.
100% Python — no Claude, no API cost.

Reads from ALL existing tables (no new data fetching) and checks 10 red flags
to compute a degradation penalty (0 to -30). This penalty can be applied as a
discount to the composite hidden gem score.

Ten Red Flags:
  1. Net insider selling > buying (90d)          -8
  2. Pledge % increasing                         -6
  3. Multiple insiders selling (>=3 people, 90d)  -5
  4. EBITDA margin declining 2+ quarters          -6
  5. Revenue declining YoY                        -5
  6. D/E increasing while margins fall            -5
  7. Auditor change (mid-term, 12m)               -8
  8. CFO/CEO resignation (12m)                    -6
  9. Credit rating downgrade (12m)                -5
 10. Price >15% below 200-DMA                     -3

Floor at -30. If degradation_score <= -15: is_degrading = True.
"""

import asyncio
import json
import structlog
from datetime import date, datetime, timedelta
from typing import Optional

log = structlog.get_logger()

# ────────────────────────────────────────────────────────────────────
# PURE COMPUTATION — no DB, no side effects
# ────────────────────────────────────────────────────────────────────

def compute_degradation_score(
    insider_signals: list[dict],
    financials_quarterly: list[dict],
    financials_annual: list[dict],
    filings: list[dict],
    company: dict,
) -> dict:
    """
    Pure function. Evaluates 10 red flags from pre-fetched data.
    Returns degradation dict ready for india_degradation_flags upsert.
    """
    red_flags: list[dict] = []

    # ── FLAG 1: Net insider selling > buying (90d) ──────────────────
    buys_cr = sum(
        (s.get("value_cr") or 0)
        for s in insider_signals
        if s.get("signal_type") == "open_market_buy"
    )
    sells_cr = sum(
        (s.get("value_cr") or 0)
        for s in insider_signals
        if s.get("signal_type") == "open_market_sell"
    )
    net_sell = sells_cr - buys_cr
    if net_sell > 0.5:
        red_flags.append({
            "flag": "net_insider_selling",
            "penalty": -8,
            "detail": f"Net selling ₹{net_sell:.2f} Cr in 90d (sell ₹{sells_cr:.2f} vs buy ₹{buys_cr:.2f})",
        })

    # ── FLAG 2: Pledge % increasing ─────────────────────────────────
    pledge_signals = [
        s for s in insider_signals
        if s.get("signal_type") == "pledge_increase"
    ]
    if pledge_signals:
        # Use the most recent pledge_increase signal
        latest_pledge = pledge_signals[0]  # already sorted desc by date
        pct_before = latest_pledge.get("pledge_pct_before")
        pct_after = latest_pledge.get("pledge_pct_after")
        if pct_before is not None and pct_after is not None and pct_after > pct_before:
            red_flags.append({
                "flag": "pledge_increasing",
                "penalty": -6,
                "detail": f"Pledge rose from {pct_before:.1f}% to {pct_after:.1f}%",
            })

    # ── FLAG 3: Multiple insiders selling (>=3 distinct people) ─────
    sellers = set()
    for s in insider_signals:
        if s.get("signal_type") == "open_market_sell" and s.get("person_name"):
            sellers.add(s["person_name"].strip().lower())
    if len(sellers) >= 3:
        red_flags.append({
            "flag": "multiple_insiders_selling",
            "penalty": -5,
            "detail": f"{len(sellers)} distinct insiders sold shares in 90d",
        })

    # ── FLAG 4: EBITDA margin declining 2+ consecutive quarters ─────
    if len(financials_quarterly) >= 3:
        margins = [
            q.get("ebitda_margin_pct")
            for q in financials_quarterly
        ]
        # Count consecutive declines (newest to oldest, series is desc)
        consecutive_declines = 0
        for i in range(len(margins) - 1):
            if margins[i] is not None and margins[i + 1] is not None:
                if margins[i] < margins[i + 1]:
                    consecutive_declines += 1
                else:
                    break
            else:
                break

        if consecutive_declines >= 2:
            first_valid = margins[0] if margins[0] is not None else "?"
            last_valid = margins[consecutive_declines] if margins[consecutive_declines] is not None else "?"
            red_flags.append({
                "flag": "ebitda_margin_declining",
                "penalty": -6,
                "detail": f"EBITDA margin declined {consecutive_declines} consecutive quarters ({last_valid}% → {first_valid}%)",
            })

    # ── FLAG 5: Revenue declining YoY ───────────────────────────────
    if len(financials_annual) >= 2:
        rev_latest = financials_annual[0].get("revenue_cr")
        rev_prev = financials_annual[1].get("revenue_cr")
        if rev_latest is not None and rev_prev is not None and rev_prev > 0:
            if rev_latest < rev_prev:
                pct_drop = ((rev_prev - rev_latest) / rev_prev) * 100
                red_flags.append({
                    "flag": "revenue_declining_yoy",
                    "penalty": -5,
                    "detail": f"Revenue fell {pct_drop:.1f}% YoY (₹{rev_prev:.0f} Cr → ₹{rev_latest:.0f} Cr)",
                })

    # ── FLAG 6: D/E increasing while margins fall ───────────────────
    current_de = company.get("debt_equity")
    if current_de is not None and len(financials_annual) >= 2:
        # Compute previous year D/E from financial history
        prev_annual = financials_annual[1]
        prev_debt = prev_annual.get("total_debt_cr")
        prev_nw = prev_annual.get("net_worth_cr")
        prev_de = (prev_debt / prev_nw) if (prev_debt is not None and prev_nw is not None and prev_nw > 0) else None

        # Check margin decline
        margin_latest = financials_annual[0].get("ebitda_margin_pct")
        margin_prev = financials_annual[1].get("ebitda_margin_pct")
        margin_falling = (
            margin_latest is not None
            and margin_prev is not None
            and margin_latest < margin_prev
        )

        if prev_de is not None and current_de > prev_de and margin_falling:
            red_flags.append({
                "flag": "leverage_up_margins_down",
                "penalty": -5,
                "detail": f"D/E rose ({prev_de:.2f} → {current_de:.2f}) while EBITDA margin fell ({margin_prev:.1f}% → {margin_latest:.1f}%)",
            })

    # ── FLAG 7: Auditor change (mid-term, 12m) ──────────────────────
    for f in filings:
        category = (f.get("category") or "").lower()
        subject = (f.get("subject_text") or "").lower()
        if "auditor" in category or ("change" in subject and "auditor" in subject):
            red_flags.append({
                "flag": "auditor_change",
                "penalty": -8,
                "detail": f"Auditor change filing detected: {(f.get('subject_text') or 'N/A')[:80]}",
            })
            break  # Only penalise once

    # ── FLAG 8: CFO/CEO resignation (12m) ───────────────────────────
    key_roles = ("cfo", "ceo", "chief financial", "chief executive", "managing director")
    for f in filings:
        subject = (f.get("subject_text") or "").lower()
        if "resignation" in subject and any(role in subject for role in key_roles):
            red_flags.append({
                "flag": "key_mgmt_resignation",
                "penalty": -6,
                "detail": f"Key management resignation: {(f.get('subject_text') or 'N/A')[:80]}",
            })
            break  # Only penalise once

    # ── FLAG 9: Credit rating downgrade (12m) ───────────────────────
    for f in filings:
        subject = (f.get("subject_text") or "").lower()
        if "credit rating" in subject and "downgrade" in subject:
            red_flags.append({
                "flag": "credit_rating_downgrade",
                "penalty": -5,
                "detail": f"Credit rating downgrade: {(f.get('subject_text') or 'N/A')[:80]}",
            })
            break  # Only penalise once

    # ── FLAG 10: Price >15% below 200-DMA ───────────────────────────
    current_price = company.get("current_price")
    two_hundred_dma = company.get("two_hundred_dma")
    if current_price is not None and two_hundred_dma is not None and two_hundred_dma > 0:
        if current_price < two_hundred_dma * 0.85:
            discount_pct = ((two_hundred_dma - current_price) / two_hundred_dma) * 100
            red_flags.append({
                "flag": "price_below_200dma",
                "penalty": -3,
                "detail": f"Price ₹{current_price:.0f} is {discount_pct:.1f}% below 200-DMA (₹{two_hundred_dma:.0f})",
            })

    # ── AGGREGATE ───────────────────────────────────────────────────
    raw_score = sum(f["penalty"] for f in red_flags)
    degradation_score = max(-30, raw_score)  # Floor at -30
    is_degrading = degradation_score <= -15

    return {
        "degradation_score": degradation_score,
        "is_degrading": is_degrading,
        "red_flags": red_flags,
        "flags_firing": len(red_flags),
        "score_narrative": _build_narrative(red_flags, degradation_score),
    }


def _build_narrative(red_flags: list[dict], score: int) -> str:
    """Human-readable summary of degradation findings."""
    if not red_flags:
        return "No degradation red flags detected"

    top = red_flags[:3]
    descs = [f["flag"].replace("_", " ").title() for f in top]

    if score <= -20:
        severity = " — SEVERE deterioration, multiple structural red flags"
    elif score <= -15:
        severity = " — Significant degradation signals, exercise caution"
    elif score <= -8:
        severity = " — Moderate warning signals present"
    else:
        severity = " — Minor concerns detected"

    remaining = len(red_flags) - len(top)
    suffix = f" (+{remaining} more)" if remaining > 0 else ""

    return "; ".join(descs) + suffix + severity


# ────────────────────────────────────────────────────────────────────
# ASYNC DB FUNCTIONS
# ────────────────────────────────────────────────────────────────────

async def monitor_company_degradation(db, isin: str, ticker: str) -> dict:
    """
    Run degradation check for one company. Fetches data from 4 tables
    in parallel, computes score, and upserts to india_degradation_flags.
    """
    cutoff_90d = (date.today() - timedelta(days=90)).isoformat()
    cutoff_12m = (date.today() - timedelta(days=365)).isoformat()

    # Parallel DB queries — insider signals, quarterly financials,
    # annual financials, corporate filings, and company record
    insider_task = db.table("india_promoter_signals") \
        .select("*") \
        .eq("isin", isin) \
        .gte("transaction_date", cutoff_90d) \
        .order("transaction_date", desc=True) \
        .execute()

    quarterly_task = db.table("india_financials_history") \
        .select("*") \
        .eq("isin", isin) \
        .eq("period_type", "quarterly") \
        .order("period_end", desc=True) \
        .limit(4) \
        .execute()

    annual_task = db.table("india_financials_history") \
        .select("*") \
        .eq("isin", isin) \
        .eq("period_type", "annual") \
        .order("period_end", desc=True) \
        .limit(2) \
        .execute()

    filings_task = db.table("india_corporate_filings") \
        .select("*") \
        .eq("isin", isin) \
        .gte("sort_date", cutoff_12m) \
        .order("sort_date", desc=True) \
        .execute()

    company_task = db.table("india_companies") \
        .select("*") \
        .eq("isin", isin) \
        .limit(1) \
        .execute()

    insider_res, quarterly_res, annual_res, filings_res, company_res = await asyncio.gather(
        insider_task, quarterly_task, annual_task, filings_task, company_task,
    )

    insider_signals = insider_res.data or []
    financials_quarterly = quarterly_res.data or []
    financials_annual = annual_res.data or []
    filings = filings_res.data or []
    company = (company_res.data or [{}])[0]

    # Compute degradation score
    score_data = compute_degradation_score(
        insider_signals, financials_quarterly, financials_annual, filings, company,
    )

    # Upsert to india_degradation_flags
    record = {
        "isin": isin,
        "ticker": ticker,
        "degradation_score": score_data["degradation_score"],
        "is_degrading": score_data["is_degrading"],
        "red_flags": json.dumps(score_data["red_flags"]),
        "flags_firing": score_data["flags_firing"],
        "score_narrative": score_data["score_narrative"],
        "scored_at": datetime.now().isoformat(),
    }

    await db.table("india_degradation_flags").upsert(
        record, on_conflict="isin"
    ).execute()

    log.debug(
        "degradation_monitored",
        ticker=ticker,
        score=score_data["degradation_score"],
        flags=score_data["flags_firing"],
    )
    return score_data


async def monitor_all_companies(db) -> dict:
    """
    Run degradation monitoring for every company in india_companies.
    Returns summary counts of monitored, degrading, and errored companies.
    """
    results = {"monitored": 0, "degrading": 0, "errors": 0}

    from india_alpha.db import fetch_all_rows
    companies = await fetch_all_rows(db, "india_companies", select="isin, ticker")
    log.info("degradation_monitor_start", companies=len(companies))

    for company in companies:
        isin = company.get("isin")
        ticker = company.get("ticker")
        if not isin or not ticker:
            continue

        try:
            score_data = await monitor_company_degradation(db, isin, ticker)
            results["monitored"] += 1
            if score_data["is_degrading"]:
                results["degrading"] += 1
        except Exception as exc:
            log.error(
                "degradation_monitor_failed",
                isin=isin,
                ticker=ticker,
                error=str(exc)[:100],
            )
            results["errors"] += 1

    log.info("degradation_monitor_complete", **results)
    return results
