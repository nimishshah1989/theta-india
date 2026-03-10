"""
smart_money_scorer.py
Computes smart_money_score (-10 to +15) from shareholding patterns and bulk deals.
100% Python — no Claude, no API cost.

Combines two data sources into an additive bonus/penalty:
  1. Shareholding patterns (india_shareholding_patterns) — superstar entries/exits,
     MF accumulation/exit, FII accumulation across quarters
  2. Bulk deals (india_bulk_deals) — institutional buys/sells within last 30 days

Nine signals, additive, capped at +15 / -10:
  Superstar new entry       +10  (name in current quarter but not previous)
  Superstar increased       +6   (same name, higher pct in current)
  Superstar exited          -8   (name in previous quarter but not current)
  MF accumulation >1% QoQ  +6   (mf_delta > 1.0)
  MF accumulation >0.5%    +3   (mf_delta > 0.5 and <= 1.0)
  MF exit >1% QoQ          -4   (mf_delta < -1.0)
  FII accumulation >0.5%   +4   (fii_delta > 0.5)
  Institutional bulk buy    +5   (is_institutional BUY in last 30 days)
  Institutional bulk sell   -5   (is_institutional SELL in last 30 days)

Reads from: india_shareholding_patterns, india_bulk_deals
Writes to:  india_smart_money_scores
"""

import asyncio
import json
import structlog
from datetime import datetime, date, timedelta
from typing import Optional

log = structlog.get_logger()


# ─── Pure scoring function ──────────────────────────────────────────


def compute_smart_money_score(
    shareholding_current: Optional[dict],
    shareholding_prev: Optional[dict],
    bulk_deals: list[dict],
) -> dict:
    """
    Pure function. Takes current/previous shareholding snapshots and recent
    bulk deals. Returns score dict ready for india_smart_money_scores.

    All inputs can be None/empty — missing data simply means fewer signals fire.
    """
    signals: list[dict] = []
    superstar_entries: list[str] = []
    superstar_exits: list[str] = []
    mf_delta: Optional[float] = None
    fii_delta: Optional[float] = None

    # ─── SHAREHOLDING SIGNALS ───────────────────────────────────────

    if shareholding_current is not None:
        # Extract superstars from current quarter's notable_holders
        current_holders_raw = shareholding_current.get("notable_holders")
        if isinstance(current_holders_raw, str):
            try:
                current_holders_raw = json.loads(current_holders_raw)
            except (json.JSONDecodeError, TypeError):
                current_holders_raw = []
        current_holders = current_holders_raw or []

        current_superstars = {
            h.get("superstar_name") or h.get("name"): h.get("pct", 0)
            for h in current_holders
            if h.get("is_superstar")
        }

        # Extract superstars from previous quarter
        prev_superstars: dict[str, float] = {}
        if shareholding_prev is not None:
            prev_holders_raw = shareholding_prev.get("notable_holders")
            if isinstance(prev_holders_raw, str):
                try:
                    prev_holders_raw = json.loads(prev_holders_raw)
                except (json.JSONDecodeError, TypeError):
                    prev_holders_raw = []
            prev_holders = prev_holders_raw or []

            prev_superstars = {
                h.get("superstar_name") or h.get("name"): h.get("pct", 0)
                for h in prev_holders
                if h.get("is_superstar")
            }

        # SIGNAL: Superstar new entry (+10)
        for name in current_superstars:
            if name not in prev_superstars:
                signals.append({
                    "signal": "superstar_new_entry",
                    "points": 10,
                    "detail": f"{name} entered with {current_superstars[name]:.2f}% stake",
                })
                superstar_entries.append(name)

        # SIGNAL: Superstar increased (+6)
        for name in current_superstars:
            if name in prev_superstars:
                if current_superstars[name] > prev_superstars[name]:
                    signals.append({
                        "signal": "superstar_increased",
                        "points": 6,
                        "detail": (
                            f"{name} increased from {prev_superstars[name]:.2f}% "
                            f"to {current_superstars[name]:.2f}%"
                        ),
                    })

        # SIGNAL: Superstar exited (-8)
        for name in prev_superstars:
            if name not in current_superstars:
                signals.append({
                    "signal": "superstar_exited",
                    "points": -8,
                    "detail": f"{name} exited (held {prev_superstars[name]:.2f}% last quarter)",
                })
                superstar_exits.append(name)

        # ─── MF / DII / FII DELTA SIGNALS ────────────────────────────

        # Prefer pre-computed deltas if available, otherwise compute from two quarters
        mf_delta = shareholding_current.get("mf_delta")
        fii_delta = shareholding_current.get("fii_delta")
        dii_delta = shareholding_current.get("dii_delta")

        # Fallback: compute deltas from current vs previous if not pre-computed
        if mf_delta is None and shareholding_prev is not None:
            mf_now = shareholding_current.get("mf_pct") or 0
            mf_prev = shareholding_prev.get("mf_pct") or 0
            mf_delta = round(mf_now - mf_prev, 2)

        if fii_delta is None and shareholding_prev is not None:
            fii_now = shareholding_current.get("fii_pct") or 0
            fii_prev = shareholding_prev.get("fii_pct") or 0
            fii_delta = round(fii_now - fii_prev, 2)

        if dii_delta is None and shareholding_prev is not None:
            dii_now = shareholding_current.get("dii_pct") or 0
            dii_prev = shareholding_prev.get("dii_pct") or 0
            dii_delta = round(dii_now - dii_prev, 2)

        # Use DII delta as proxy for MF when MF-specific data isn't available
        # Screener.in provides DII (includes MF + insurance + banks) but not MF alone
        institutional_delta = mf_delta if (mf_delta is not None and mf_delta != 0) else dii_delta

        # SIGNAL: DII/MF accumulation >1% QoQ (+6)
        if institutional_delta is not None and institutional_delta > 1.0:
            label = "Mutual fund" if (mf_delta and mf_delta != 0) else "DII"
            signals.append({
                "signal": "dii_accumulation_strong",
                "points": 6,
                "detail": f"{label} holding increased by {institutional_delta:.2f}pp QoQ",
            })
        # SIGNAL: DII/MF accumulation >0.5% QoQ (+3) — don't double count
        elif institutional_delta is not None and institutional_delta > 0.5:
            label = "Mutual fund" if (mf_delta and mf_delta != 0) else "DII"
            signals.append({
                "signal": "dii_accumulation_moderate",
                "points": 3,
                "detail": f"{label} holding increased by {institutional_delta:.2f}pp QoQ",
            })

        # SIGNAL: DII/MF exit >1% QoQ (-4)
        if institutional_delta is not None and institutional_delta < -1.0:
            label = "Mutual fund" if (mf_delta and mf_delta != 0) else "DII"
            signals.append({
                "signal": "dii_exit",
                "points": -4,
                "detail": f"{label} holding decreased by {abs(institutional_delta):.2f}pp QoQ",
            })

        # SIGNAL: FII accumulation >0.5% QoQ (+4)
        if fii_delta is not None and fii_delta > 0.5:
            signals.append({
                "signal": "fii_accumulation",
                "points": 4,
                "detail": f"FII holding increased by {fii_delta:.2f}pp QoQ",
            })

        # SIGNAL: FII exit >0.5% QoQ (-3)
        if fii_delta is not None and fii_delta < -0.5:
            signals.append({
                "signal": "fii_exit",
                "points": -3,
                "detail": f"FII holding decreased by {abs(fii_delta):.2f}pp QoQ",
            })

    # ─── BULK DEAL SIGNALS ──────────────────────────────────────────

    institutional_buys = [
        d for d in bulk_deals
        if d.get("is_institutional") and (d.get("buy_sell") or "").upper() == "BUY"
    ]
    institutional_sells = [
        d for d in bulk_deals
        if d.get("is_institutional") and (d.get("buy_sell") or "").upper() == "SELL"
    ]

    # SIGNAL: Institutional bulk buy in last 30 days (+5)
    if institutional_buys:
        top_buy = max(institutional_buys, key=lambda d: d.get("value_cr", 0) or 0)
        signals.append({
            "signal": "institutional_bulk_buy",
            "points": 5,
            "detail": (
                f"{top_buy.get('client_name', 'Institution')} bought "
                f"{top_buy.get('value_cr', 0):.2f} Cr on {top_buy.get('trade_date', '?')}"
            ),
        })

    # SIGNAL: Institutional bulk sell in last 30 days (-5)
    if institutional_sells:
        top_sell = max(institutional_sells, key=lambda d: d.get("value_cr", 0) or 0)
        signals.append({
            "signal": "institutional_bulk_sell",
            "points": -5,
            "detail": (
                f"{top_sell.get('client_name', 'Institution')} sold "
                f"{top_sell.get('value_cr', 0):.2f} Cr on {top_sell.get('trade_date', '?')}"
            ),
        })

    # ─── AGGREGATE ──────────────────────────────────────────────────

    raw_total = sum(s["points"] for s in signals)
    capped_score = max(min(raw_total, 15), -10)

    return {
        "smart_money_score": capped_score,
        "signals": signals,
        "signals_firing": len(signals),
        "superstar_entries": superstar_entries,
        "superstar_exits": superstar_exits,
        "mf_delta": mf_delta,
        "fii_delta": fii_delta,
        "dii_delta": dii_delta,
        "score_narrative": _build_smart_money_narrative(signals, capped_score),
    }


# ─── Narrative builder ──────────────────────────────────────────────


def _build_smart_money_narrative(signals: list[dict], score: int) -> str:
    """Summarize smart money findings into a one-line narrative."""
    if not signals:
        return "No smart money signals detected"

    # Pick top positive and top negative signal by absolute points
    positives = [s for s in signals if s["points"] > 0]
    negatives = [s for s in signals if s["points"] < 0]

    parts = []
    if positives:
        top_pos = max(positives, key=lambda s: s["points"])
        parts.append(top_pos["detail"])
    if negatives:
        top_neg = min(negatives, key=lambda s: s["points"])
        parts.append(top_neg["detail"])

    narrative = "; ".join(parts)

    if score >= 10:
        narrative += " -> Strong smart money accumulation"
    elif score >= 5:
        narrative += " -> Net positive institutional interest"
    elif score <= -5:
        narrative += " -> CAUTION: smart money exiting"

    return narrative


# ─── Async DB functions ─────────────────────────────────────────────


async def score_company_smart_money(db, isin: str, ticker: str) -> dict:
    """
    Score one company's smart money signals.
    Queries shareholding patterns (last 2 quarters) and bulk deals (last 30 days)
    in parallel, then computes and upserts the score.
    """
    cutoff_30d = (date.today() - timedelta(days=30)).isoformat()

    # Run both queries concurrently
    shareholding_task = db.table("india_shareholding_patterns") \
        .select("*") \
        .eq("isin", isin) \
        .order("quarter", desc=True) \
        .limit(2) \
        .execute()

    bulk_deals_task = db.table("india_bulk_deals") \
        .select("*") \
        .eq("ticker", ticker) \
        .gte("trade_date", cutoff_30d) \
        .eq("is_institutional", True) \
        .execute()

    shareholding_result, bulk_deals_result = await asyncio.gather(
        shareholding_task, bulk_deals_task
    )

    shareholding_rows = shareholding_result.data or []
    bulk_deals = bulk_deals_result.data or []

    # Split into current and previous quarter
    shareholding_current = shareholding_rows[0] if len(shareholding_rows) >= 1 else None
    shareholding_prev = shareholding_rows[1] if len(shareholding_rows) >= 2 else None

    score_data = compute_smart_money_score(
        shareholding_current, shareholding_prev, bulk_deals
    )

    record = {
        "isin": isin,
        "ticker": ticker,
        "smart_money_score": score_data["smart_money_score"],
        "signals": json.dumps(score_data["signals"]),
        "signals_firing": score_data["signals_firing"],
        "superstar_entries": json.dumps(score_data["superstar_entries"]),
        "superstar_exits": json.dumps(score_data["superstar_exits"]),
        "mf_delta": score_data["mf_delta"],
        "fii_delta": score_data["fii_delta"],
        "score_narrative": score_data["score_narrative"],
        "scored_at": datetime.now().isoformat(),
    }

    await db.table("india_smart_money_scores").upsert(
        record, on_conflict="isin"
    ).execute()

    log.debug("smart_money_scored", ticker=ticker,
              score=score_data["smart_money_score"],
              signals=score_data["signals_firing"])
    return score_data


async def score_all_companies_smart_money(db) -> dict:
    """
    Score smart money signals for all companies that have either
    shareholding data or bulk deal data. Deduplicates by ISIN.
    Returns summary: {"scored": N, "no_data": N, "errors": N}.
    """
    results = {"scored": 0, "no_data": 0, "errors": 0}

    # Get ISINs from both data sources in parallel
    from india_alpha.db import fetch_all_rows
    shareholding_rows = await fetch_all_rows(
        db, "india_shareholding_patterns", select="isin, ticker"
    )
    bulk_deals_rows = await fetch_all_rows(
        db, "india_bulk_deals", select="isin, ticker"
    )

    # Deduplicate by ISIN — prefer ticker from shareholding if both exist
    isin_ticker_map = {}
    for row in bulk_deals_rows:
        isin = row.get("isin")
        ticker = row.get("ticker")
        if isin and ticker:
            isin_ticker_map[isin] = ticker

    for row in shareholding_rows:
        isin = row.get("isin")
        ticker = row.get("ticker")
        if isin and ticker:
            isin_ticker_map[isin] = ticker

    companies = [{"isin": isin, "ticker": ticker} for isin, ticker in isin_ticker_map.items()]

    if not companies:
        log.warning("smart_money_no_companies", msg="No shareholding or bulk deal data found")
        return results

    log.info("smart_money_scoring_start", companies=len(companies))

    for company in companies:
        try:
            score_data = await score_company_smart_money(
                db, company["isin"], company["ticker"]
            )
            if score_data["signals_firing"] == 0:
                results["no_data"] += 1
            else:
                results["scored"] += 1
        except Exception as exc:
            log.error("smart_money_score_failed",
                      isin=company["isin"], error=str(exc)[:100])
            results["errors"] += 1

    log.info("smart_money_scoring_complete", **results)
    return results
