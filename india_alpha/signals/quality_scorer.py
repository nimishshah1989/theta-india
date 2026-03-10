"""
quality_scorer.py
Computes quality_emergence_score (0-100) from financial history.
100% Python — no Claude, no API cost.

Six signals that detect phase-change in fundamental quality:
  1. ROE Breakout — ROE crosses 15% from below
  2. ROCE Consistency — ROCE >15% for 2+ consecutive years AND improving
  3. Margin Expansion Streak — EBITDA margin expanding 3+ years
  4. Deleveraging — Debt/Equity falling below 0.5 from above 0.8
  5. Working Capital Tightening — Debtor+Inventory days declining 3 years
  6. Earnings Quality — PAT margin expanding + revenue growing >10% CAGR

When 4+ signals fire → 1.15x multiplier (quality is reinforcing)
"""

import asyncio
import structlog
from datetime import datetime
from typing import Optional

log = structlog.get_logger()


def compute_quality_score(history: list[dict]) -> dict:
    """
    Pure function. Takes list of annual financial periods (newest first).
    Returns score dict ready for india_quality_scores.
    Requires minimum 3 years to detect trends.
    """
    if len(history) < 3:
        return _empty_score()

    current = history[0]
    prev = history[1]
    oldest = history[-1]

    # Build multi-year series for streak detection
    roe_series = [h.get("roe") for h in history if h.get("roe") is not None]
    roce_series = [h.get("roce") for h in history if h.get("roce") is not None]
    margin_series = [h.get("ebitda_margin_pct") for h in history if h.get("ebitda_margin_pct") is not None]

    score = 0
    signals = []

    # ─── SIGNAL 1: ROE Breakout (+20) ────────────────────────────────
    # ROE crosses 15% from below (3yr avg was <13%)
    roe_now = current.get("roe") or 0
    if len(roe_series) >= 3:
        roe_avg_prior = sum(roe_series[1:]) / len(roe_series[1:])
        if roe_now >= 15 and roe_avg_prior < 13:
            score += 20
            signals.append({
                "signal": "roe_breakout",
                "description": f"ROE crossed 15% ({roe_now:.1f}%), prior avg was {roe_avg_prior:.1f}%",
                "implication": "Equity returns crossing quality threshold — phase change in profitability",
                "magnitude": "HIGH",
            })

    # ─── SIGNAL 2: ROCE Consistency (+18) ────────────────────────────
    # ROCE >15% for 2+ consecutive years AND improving
    if len(roce_series) >= 2:
        roce_now = roce_series[0] or 0
        roce_prev = roce_series[1] or 0
        consecutive_above_15 = 0
        for r in roce_series:
            if r and r >= 15:
                consecutive_above_15 += 1
            else:
                break

        if consecutive_above_15 >= 2 and roce_now > roce_prev:
            score += 18
            signals.append({
                "signal": "roce_consistency",
                "description": f"ROCE >15% for {consecutive_above_15} consecutive years, improving ({roce_prev:.1f}% → {roce_now:.1f}%)",
                "implication": "Sustained capital efficiency — competitive moat building",
                "magnitude": "HIGH",
            })

    # ─── SIGNAL 3: Margin Expansion Streak (+18) ─────────────────────
    # EBITDA margin expanding 3+ consecutive years, delta >3pp
    if len(margin_series) >= 3:
        expanding_streak = 0
        for i in range(len(margin_series) - 1):
            if margin_series[i] is not None and margin_series[i + 1] is not None:
                if margin_series[i] > margin_series[i + 1]:
                    expanding_streak += 1
                else:
                    break
            else:
                break

        margin_now = margin_series[0] or 0
        # Use the value at the end of the streak for delta calculation
        margin_start_idx = min(expanding_streak, len(margin_series) - 1)
        margin_start = margin_series[margin_start_idx] or 0
        total_delta = margin_now - margin_start

        if expanding_streak >= 3 and total_delta > 3:
            score += 18
            signals.append({
                "signal": "margin_expansion_streak",
                "description": f"EBITDA margin expanding {expanding_streak} consecutive years, +{total_delta:.1f}pp ({margin_start:.1f}% → {margin_now:.1f}%)",
                "implication": "Structural margin improvement — pricing power or scale advantages materialising",
                "magnitude": "HIGH",
            })
        elif expanding_streak >= 2 and total_delta > 2:
            score += 10

    # ─── SIGNAL 4: Deleveraging (+16) ────────────────────────────────
    # Debt/Equity falling below 0.5 from above 0.8 within available history
    debt_now = current.get("total_debt_cr") or 0
    nw_now = current.get("net_worth_cr") or 0
    de_now = (debt_now / nw_now) if nw_now > 0 else 0

    # Check historical D/E — find peak D/E
    peak_de = 0
    for h in history[1:]:
        h_debt = h.get("total_debt_cr") or 0
        h_nw = h.get("net_worth_cr") or 0
        if h_nw > 0:
            h_de = h_debt / h_nw
            peak_de = max(peak_de, h_de)

    if de_now < 0.5 and peak_de > 0.8:
        score += 16
        signals.append({
            "signal": "deleveraging",
            "description": f"D/E ratio fell from {peak_de:.2f}x to {de_now:.2f}x (below 0.5x threshold)",
            "implication": "Balance sheet transformation — financial risk dramatically reduced",
            "magnitude": "HIGH",
        })

    # ─── SIGNAL 5: Working Capital Tightening (+14) ──────────────────
    # (Debtor days + Inventory days) declining 3 years straight
    wc_series = []
    for h in history:
        debtor_d = h.get("debtor_days")
        inv_d = h.get("inventory_days")
        if debtor_d is not None and inv_d is not None:
            wc_series.append(debtor_d + inv_d)

    if len(wc_series) >= 3:
        declining_streak = 0
        for i in range(len(wc_series) - 1):
            if wc_series[i] < wc_series[i + 1]:
                declining_streak += 1
            else:
                break

        if declining_streak >= 3:
            delta = wc_series[declining_streak] - wc_series[0]
            score += 14
            signals.append({
                "signal": "working_capital_tightening",
                "description": f"Working capital cycle (debtor+inventory days) declining {declining_streak} years, -{delta:.0f} days",
                "implication": "Cash conversion improving — less capital locked in operations",
                "magnitude": "MEDIUM",
            })
        elif declining_streak >= 2:
            score += 7

    # ─── SIGNAL 6: Earnings Quality (+14) ────────────────────────────
    # PAT margin expanding while revenue also growing >10% CAGR
    pat_margin_now = current.get("pat_margin_pct") or 0
    pat_margin_oldest = oldest.get("pat_margin_pct") or 0
    rev_now = current.get("revenue_cr") or 0
    rev_oldest = oldest.get("revenue_cr") or 0
    years = len(history) - 1

    pat_margin_expanding = pat_margin_now > pat_margin_oldest + 1  # At least 1pp
    rev_cagr = 0
    if rev_oldest > 0 and rev_now > 0 and years > 0:
        rev_cagr = (rev_now / rev_oldest) ** (1 / years) - 1

    if pat_margin_expanding and rev_cagr > 0.10:
        score += 14
        signals.append({
            "signal": "earnings_quality",
            "description": f"PAT margin +{pat_margin_now - pat_margin_oldest:.1f}pp ({pat_margin_oldest:.1f}% → {pat_margin_now:.1f}%) with revenue CAGR {rev_cagr*100:.0f}%",
            "implication": "Profitable growth — not sacrificing margins for revenue, earnings quality improving",
            "magnitude": "HIGH",
        })

    # ─── MULTIPLIER: 4+ signals firing ───────────────────────────────
    if len(signals) >= 4:
        score = int(score * 1.15)

    final_score = max(0, min(100, score))

    return {
        "quality_score": final_score,
        "signals_firing": len(signals),
        "active_signals": signals,
        "score_narrative": _build_quality_narrative(signals, final_score),
    }


def _build_quality_narrative(signals: list, score: int) -> str:
    if not signals:
        return "No quality emergence signals detected"

    top = signals[:2]
    descs = [s["description"] for s in top]

    if score >= 65:
        suffix = " → Quality transformation underway across multiple dimensions"
    elif score >= 40:
        suffix = " → Quality emergence signals strengthening"
    elif score >= 20:
        suffix = " → Early quality improvement signals"
    else:
        suffix = ""

    return "; ".join(descs) + suffix


def _empty_score() -> dict:
    return {
        "quality_score": 0,
        "signals_firing": 0,
        "active_signals": [],
        "score_narrative": "Insufficient financial history (<3 years)",
    }


async def score_company_quality(db, isin: str, ticker: str) -> dict:
    """Score one company's quality emergence. Reads from DB, writes score."""
    result = await db.table("india_financials_history") \
        .select("*") \
        .eq("isin", isin) \
        .eq("period_type", "annual") \
        .order("period_end", desc=True) \
        .limit(10) \
        .execute()

    history = result.data or []
    score_data = compute_quality_score(history)

    record = {
        "isin": isin,
        "ticker": ticker,
        **score_data,
        "scored_at": datetime.now().isoformat(),
    }

    await db.table("india_quality_scores").upsert(
        record, on_conflict="isin"
    ).execute()

    log.debug("quality_scored", ticker=ticker, score=score_data["quality_score"],
              signals=score_data["signals_firing"])
    return score_data


async def score_all_companies_quality(db) -> dict:
    """Score quality emergence for all companies with financial history."""
    results = {"scored": 0, "no_data": 0, "errors": 0}

    # Get all ISINs that have financial history (paginated)
    from india_alpha.db import fetch_all_rows
    financials_rows = await fetch_all_rows(
        db, "india_financials_history", select="isin, ticker",
        eq={"period_type": "annual"}
    )

    seen = set()
    companies = []
    for row in financials_rows:
        if row["isin"] not in seen:
            seen.add(row["isin"])
            companies.append(row)

    log.info("quality_scoring_start", companies=len(companies))

    for company in companies:
        try:
            await score_company_quality(db, company["isin"], company["ticker"])
            results["scored"] += 1
        except Exception as e:
            log.error("quality_score_failed",
                      isin=company["isin"], error=str(e)[:100])
            results["errors"] += 1

    log.info("quality_scoring_complete", **results)
    return results
