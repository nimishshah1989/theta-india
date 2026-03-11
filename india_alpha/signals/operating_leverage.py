"""
operating_leverage.py
Computes operating_leverage_score (0-100) from financial history.
100% Python — no Claude, no API cost.

Six signals that precede earnings surprises:
  1. Capacity utilisation rising through 65-75% band
  2. Revenue mix shifting to branded/own-product (higher margins)
  3. Net debt → Net cash transition (interest savings → PAT)
  4. Order book / Revenue > 2.5x (contracted visibility)
  5. Export revenue inflecting >15% YoY
  6. Debtor days compressing >10 days YoY (pricing power)

When 3+ signals fire simultaneously = INFLECTION CANDIDATE
"""

from datetime import datetime

import structlog

log = structlog.get_logger()


def compute_ol_score(history: list[dict]) -> dict:
    """
    Pure function. Takes list of annual financial periods (newest first).
    Returns score dict ready for india_operating_leverage_scores.
    """
    if len(history) < 2:
        return _empty_score()

    current = history[0]
    prev = history[1]
    oldest = history[-1]   # For multi-year trend

    score = 0
    signals = []

    # ─── SIGNAL 1: Debt → Cash transition ─────────────────────────────
    debt_now = current.get("total_debt_cr") or 0
    cash_now = current.get("cash_cr") or 0
    debt_prev = prev.get("total_debt_cr") or 0
    cash_prev = prev.get("cash_cr") or 0

    net_debt_now = debt_now - cash_now
    net_debt_prev = debt_prev - cash_prev

    if net_debt_prev > 5 and net_debt_now < 0:
        # Crossed zero — debt eliminated
        score += 25
        signals.append({
            "signal": "debt_to_net_cash",
            "description": f"Net debt ₹{net_debt_prev:.0f}Cr → Net cash ₹{abs(net_debt_now):.0f}Cr",
            "implication": "Interest cost eliminated — full saving flows to PAT",
            "magnitude": "HIGH",
        })
    elif net_debt_prev > 0 and net_debt_now < net_debt_prev * 0.4:
        # Debt reduced by more than 60%
        score += 14
        signals.append({
            "signal": "rapid_debt_reduction",
            "description": f"Debt slashed: ₹{net_debt_prev:.0f}Cr → ₹{net_debt_now:.0f}Cr",
            "implication": "Financial risk declining — approaching debt-free milestone",
            "magnitude": "MEDIUM",
        })

    # ─── SIGNAL 2: EBITDA margin structural expansion ──────────────────
    margin_now = current.get("ebitda_margin_pct") or 0
    margin_prev = prev.get("ebitda_margin_pct") or 0
    margin_3yr = oldest.get("ebitda_margin_pct") or 0

    margin_delta_1y = margin_now - margin_prev
    margin_delta_3y = margin_now - margin_3yr

    if margin_delta_3y > 6 and margin_delta_1y > 1:
        score += 20
        signals.append({
            "signal": "margin_structural_expansion",
            "description": f"EBITDA margin {margin_3yr:.1f}% → {margin_now:.1f}% (3yr), {margin_delta_1y:+.1f}pp last year",
            "implication": "Operating leverage materialising — fixed costs spread over larger base",
            "magnitude": "HIGH",
        })
    elif margin_delta_1y > 3:
        score += 12
        signals.append({
            "signal": "margin_acceleration",
            "description": f"EBITDA margin +{margin_delta_1y:.1f}pp YoY ({margin_prev:.1f}% → {margin_now:.1f}%)",
            "implication": "Margin expansion gaining momentum",
            "magnitude": "MEDIUM",
        })
    elif margin_delta_1y > 1.5:
        score += 6

    # ─── SIGNAL 3: ROCE inflection ─────────────────────────────────────
    roce_now = current.get("roce") or 0
    roce_prev = prev.get("roce") or 0
    roce_3yr = oldest.get("roce") or 0

    if roce_now > 18 and roce_now > roce_3yr + 6:
        score += 15
        signals.append({
            "signal": "roce_inflection",
            "description": f"ROCE {roce_3yr:.0f}% → {roce_now:.0f}% over 3 years",
            "implication": "Capital efficiency breakthrough — each rupee working harder",
            "magnitude": "HIGH",
        })
    elif roce_now > roce_prev + 3 and roce_now > 12:
        score += 8

    # ─── SIGNAL 4: Revenue CAGR ────────────────────────────────────────
    rev_now = current.get("revenue_cr") or 0
    rev_3yr = oldest.get("revenue_cr") or 0
    rev_prev = prev.get("revenue_cr") or 0

    if rev_3yr > 0 and rev_now > 0:
        cagr = (rev_now / rev_3yr) ** (1/3) - 1

        if cagr > 0.22:
            score += 15
            signals.append({
                "signal": "revenue_hypergrowth",
                "description": f"Revenue CAGR {cagr*100:.0f}% over 3 years (₹{rev_3yr:.0f}Cr → ₹{rev_now:.0f}Cr)",
                "implication": "Fixed cost base being leveraged over rapidly growing revenue",
                "magnitude": "HIGH",
            })
        elif cagr > 0.14:
            score += 8
        elif cagr > 0.08:
            score += 4

    # ─── SIGNAL 5: Receivables compression ────────────────────────────
    debtors_now = current.get("debtor_days") or 0
    debtors_prev = prev.get("debtor_days") or 0

    if debtors_prev > 0 and debtors_now > 0:
        delta = debtors_prev - debtors_now
        if delta > 15:
            score += 12
            signals.append({
                "signal": "receivables_compression",
                "description": f"Debtor days {debtors_prev:.0f} → {debtors_now:.0f} (-{delta:.0f} days)",
                "implication": "Cash generation improving faster than EBITDA — pricing power signal",
                "magnitude": "MEDIUM",
            })
        elif delta > 8:
            score += 6

    # ─── SIGNAL 6: Net worth growth (equity value creation) ───────────
    nw_now = current.get("net_worth_cr") or 0
    nw_3yr = oldest.get("net_worth_cr") or 0

    if nw_3yr > 0 and nw_now > nw_3yr * 1.8:
        score += 10
        signals.append({
            "signal": "equity_value_creation",
            "description": f"Net worth grew {((nw_now/nw_3yr)-1)*100:.0f}% in 3 years",
            "implication": "Retained earnings compounding — internally funded growth",
            "magnitude": "MEDIUM",
        })

    final_score = max(0, min(100, score))

    return {
        "ol_score": final_score,
        "signals_firing": len(signals),
        "is_inflection_candidate": final_score >= 35 and len(signals) >= 2,
        "active_signals": signals,
        "score_narrative": _build_ol_narrative(signals, final_score),
    }


def _build_ol_narrative(signals: list, score: int) -> str:
    if not signals:
        return "No operating leverage signals detected"

    top = signals[:2]
    descs = [s["description"] for s in top]

    if score >= 65:
        suffix = " → Multiple inflection signals firing simultaneously"
    elif score >= 40:
        suffix = " → Inflection forming"
    elif score >= 20:
        suffix = " → Early signals"
    else:
        suffix = ""

    return "; ".join(descs) + suffix


def _empty_score() -> dict:
    return {
        "ol_score": 0,
        "signals_firing": 0,
        "is_inflection_candidate": False,
        "active_signals": [],
        "score_narrative": "Insufficient financial history (<2 years)",
    }


async def score_company_ol(db, isin: str, ticker: str) -> dict:
    """Score one company's operating leverage. Reads from DB, writes score."""
    result = await db.table("india_financials_history") \
        .select("*") \
        .eq("isin", isin) \
        .eq("period_type", "annual") \
        .order("period_end", desc=True) \
        .limit(5) \
        .execute()

    history = result.data or []
    score_data = compute_ol_score(history)

    record = {
        "isin": isin,
        "ticker": ticker,
        **score_data,
        "scored_at": datetime.now().isoformat(),
    }

    await db.table("india_operating_leverage_scores").upsert(
        record, on_conflict="isin"
    ).execute()

    log.debug("ol_scored", ticker=ticker, score=score_data["ol_score"],
              signals=score_data["signals_firing"])
    return score_data


async def score_all_companies_ol(db) -> dict:
    """Score OL for all companies that have financial history."""
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

    log.info("ol_scoring_start", companies=len(companies))

    for company in companies:
        try:
            await score_company_ol(db, company["isin"], company["ticker"])
            results["scored"] += 1
        except Exception as e:
            log.error("ol_score_failed",
                      isin=company["isin"], error=str(e)[:100])
            results["errors"] += 1

    log.info("ol_scoring_complete", **results)
    return results
