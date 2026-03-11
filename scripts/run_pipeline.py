"""
run_pipeline.py
Master pipeline runner for JIP Horizon India.
Runs all 15 signal steps in sequence and produces the daily hidden gems output.

Usage:
    python scripts/run_pipeline.py --step all
    python scripts/run_pipeline.py --step universe
    python scripts/run_pipeline.py --step insider
    python scripts/run_pipeline.py --step financials
    python scripts/run_pipeline.py --step quality
    python scripts/run_pipeline.py --step policy
    python scripts/run_pipeline.py --step corporate
    python scripts/run_pipeline.py --step enrich
    python scripts/run_pipeline.py --step valuation
    python scripts/run_pipeline.py --step smartmoney
    python scripts/run_pipeline.py --step degradation
    python scripts/run_pipeline.py --step score
    python scripts/run_pipeline.py --step output
    python scripts/run_pipeline.py --step verify
"""

import asyncio
import argparse
import sys
import os
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structlog
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True)
    ]
)
log = structlog.get_logger()


async def step_universe(db, settings, max_symbols=300):
    """Build company universe from NSE."""
    from india_alpha.fetchers.universe_builder import build_universe
    log.info("═══ STEP 1: UNIVERSE BUILD ═══")
    result = await build_universe(db, max_symbols=max_symbols)
    log.info("universe_complete", **result)
    return result


async def step_screener_enrich(db, settings):
    """Enrich all companies with reliable Screener.in valuation data,
    then recover companies that yfinance missed."""
    from india_alpha.fetchers.screener_enricher import enrich_all_companies, enrich_and_add_missing
    from india_alpha.fetchers.universe_builder import fetch_nse_symbols
    log.info("═══ STEP 1b: SCREENER.IN ENRICHMENT ═══")

    if not settings.screener_session_cookie:
        log.warning("screener_enrich_skipped",
                    msg="No SCREENER_SESSION_COOKIE in .env — skipping")
        return {"skipped": True, "reason": "no_cookie"}

    # Phase 1: Enrich existing companies with Screener.in data
    enrich_result = await enrich_all_companies(db, settings.screener_session_cookie)
    log.info("screener_enrichment_complete", **enrich_result)

    # Phase 2: Recover companies that yfinance missed entirely
    log.info("═══ STEP 1c: RECOVERING MISSED COMPANIES ═══")
    all_nse_symbols = await fetch_nse_symbols()

    # Find which symbols are NOT yet in the DB (paginated to get all)
    from india_alpha.db import fetch_all_rows
    db_rows = await fetch_all_rows(db, "india_companies", select="ticker")
    existing_tickers = {c["ticker"] for c in db_rows}

    missing_symbols = [
        s for s in all_nse_symbols
        if s["nse_symbol"] not in existing_tickers
    ]
    log.info("recovery_check",
             total_nse=len(all_nse_symbols),
             in_db=len(existing_tickers),
             missing=len(missing_symbols))

    recovery_result = {"checked": 0, "added": 0}
    if missing_symbols:
        recovery_result = await enrich_and_add_missing(
            db, settings.screener_session_cookie, missing_symbols
        )
        log.info("recovery_complete", **recovery_result)

    return {
        "enriched": enrich_result.get("enriched", 0),
        "recovered": recovery_result.get("added", 0),
        "total_missed": len(missing_symbols),
    }


async def step_insider(db, days_back=365):
    """Fetch NSE insider trading signals for all companies."""
    from india_alpha.fetchers.nse_insider_fetcher import fetch_and_store_insider_signals
    log.info("═══ STEP 2: NSE INSIDER SIGNALS ═══")
    result = await fetch_and_store_insider_signals(db, days_back=days_back)
    log.info("insider_complete", **result)
    return result


async def step_promoter_score(db):
    """Score promoter signals for all companies."""
    from india_alpha.signals.promoter_scorer import score_all_companies
    log.info("═══ STEP 3: PROMOTER SCORING ═══")
    result = await score_all_companies(db)
    log.info("promoter_scoring_complete", **result)
    return result


async def step_financials(db, settings, max_companies=2000):
    """Fetch Screener.in financials for companies in universe."""
    from india_alpha.fetchers.screener_fetcher import batch_fetch_financials

    log.info("═══ STEP 4: SCREENER.IN FINANCIALS ═══")

    if not settings.screener_session_cookie:
        log.warning("screener_skipped",
                    msg="No SCREENER_SESSION_COOKIE in .env — skipping Screener fetch",
                    tip="Get cookie: screener.in → F12 → Cookies → sessionid")
        return {"skipped": True, "reason": "no_cookie"}

    # Resume support: skip companies that already have financial data
    from india_alpha.db import fetch_all_rows
    existing_rows = await fetch_all_rows(
        db, "india_financials_history",
        select="isin",
        eq={"period_type": "annual"},
    )
    existing_isins = {r["isin"] for r in existing_rows}

    # Get all companies, ordered by market_cap DESC (most important first)
    all_companies = await fetch_all_rows(
        db, "india_companies",
        select="ticker, isin, market_cap_cr",
    )

    # Sort by market cap descending (most important first, so interrupted runs still have best data)
    all_companies.sort(key=lambda c: c.get("market_cap_cr") or 0, reverse=True)

    # Filter out companies we already have data for
    companies = [
        c for c in all_companies
        if c.get("isin") and c["isin"] not in existing_isins
    ][:max_companies]

    log.info("fetching_financials",
             total_universe=len(all_companies),
             already_have=len(existing_isins),
             to_fetch=len(companies))

    if not companies:
        log.info("financials_all_fetched", msg="All companies already have financial data")
        return {"processed": 0, "success": 0, "failed": 0, "already_have": len(existing_isins)}

    result = await batch_fetch_financials(
        db, companies, settings.screener_session_cookie
    )
    result["already_have"] = len(existing_isins)
    log.info("financials_complete", **result)
    return result


async def step_ol_score(db):
    """Compute operating leverage scores."""
    from india_alpha.signals.operating_leverage import score_all_companies_ol
    log.info("═══ STEP 5: OPERATING LEVERAGE SCORING ═══")
    result = await score_all_companies_ol(db)
    log.info("ol_scoring_complete", **result)
    return result


async def step_quality_score(db):
    """Compute quality emergence scores."""
    from india_alpha.signals.quality_scorer import score_all_companies_quality
    log.info("═══ STEP 6: QUALITY EMERGENCE SCORING ═══")
    result = await score_all_companies_quality(db)
    log.info("quality_scoring_complete", **result)
    return result


async def step_policy_score(db):
    """Compute policy tailwind scores."""
    from india_alpha.signals.policy_scorer import score_all_companies_policy
    log.info("═══ STEP 7: POLICY TAILWIND SCORING ═══")
    result = await score_all_companies_policy(db)
    log.info("policy_scoring_complete", **result)
    return result


async def step_corporate_filings_fetch(db, settings, max_companies=2000, fetch_all_companies=False):
    """Fetch corporate filings from NSE (free, no API key needed)."""
    from india_alpha.fetchers.nse_filings_fetcher import fetch_and_store_filings
    log.info("═══ STEP 8: NSE CORPORATE FILINGS FETCH ═══")

    result = await fetch_and_store_filings(
        db,
        max_companies=max_companies,
        fetch_all=fetch_all_companies,
        skip_pdf_download=fetch_all_companies,  # Skip PDFs on bulk fetch
    )
    log.info("corporate_filings_fetch_complete", **result)
    return result


async def step_corporate_intelligence_score(db, settings, cost_tracker=None):
    """Score corporate filings (Python rules + Claude for top filings)."""
    from india_alpha.signals.corporate_intelligence_scorer import score_all_companies
    log.info("═══ STEP 9: CORPORATE INTELLIGENCE SCORING ═══")

    claude_client = None
    if settings.anthropic_api_key:
        import anthropic
        claude_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    result = await score_all_companies(
        db, claude_client, settings.claude_model, cost_tracker
    )
    log.info("corporate_intelligence_scoring_complete", **result)
    return result


async def step_valuation_score(db):
    """Compute valuation scores for all companies."""
    from india_alpha.signals.valuation_scorer import score_all_companies_valuation
    log.info("═══ STEP 10: VALUATION SCORING ═══")
    result = await score_all_companies_valuation(db)
    log.info("valuation_scoring_complete", **result)
    return result


async def step_smart_money_fetch(db, settings=None):
    """Fetch bulk deals + shareholding patterns (Screener.in for shareholding, NSE for bulk deals)."""
    from india_alpha.fetchers.nse_bulk_deals_fetcher import fetch_and_store_bulk_deals
    from india_alpha.fetchers.screener_fetcher import batch_fetch_shareholding
    from india_alpha.db import fetch_all_rows
    log.info("═══ STEP 11: SMART MONEY DATA FETCH ═══")

    bulk_result = await fetch_and_store_bulk_deals(db)
    log.info("bulk_deals_complete", **bulk_result)

    # Fetch shareholding from Screener.in (provides FII/DII/public breakdown)
    cookie = (settings.screener_session_cookie if settings else "") or ""
    if not cookie:
        from india_alpha.config import get_settings
        cookie = get_settings().screener_session_cookie

    if not cookie:
        log.warning("shareholding_skipped", msg="No SCREENER_SESSION_COOKIE — can't fetch shareholding")
        return {"bulk_deals": bulk_result, "shareholding": {"skipped": True}}

    # Resume support: skip companies that already have shareholding with fii_pct > 0
    all_companies = await fetch_all_rows(db, "india_companies", select="ticker, isin, market_cap_cr")
    all_companies.sort(key=lambda c: c.get("market_cap_cr") or 0, reverse=True)

    existing_rows = await fetch_all_rows(
        db, "india_shareholding_patterns",
        select="isin, fii_pct",
    )
    # Only skip if we already have FII data (not the NSE zeros)
    existing_isins = {r["isin"] for r in existing_rows if (r.get("fii_pct") or 0) > 0.01}

    companies = [
        c for c in all_companies
        if c.get("isin") and c["isin"] not in existing_isins
    ]

    log.info("shareholding_fetch_start",
             total=len(all_companies),
             already_have_fii=len(existing_isins),
             to_fetch=len(companies))

    shareholding_result = await batch_fetch_shareholding(db, companies, cookie)
    log.info("shareholding_complete", **shareholding_result)

    return {"bulk_deals": bulk_result, "shareholding": shareholding_result}


async def step_smart_money_score(db):
    """Compute smart money scores."""
    from india_alpha.signals.smart_money_scorer import score_all_companies_smart_money
    log.info("═══ STEP 12: SMART MONEY SCORING ═══")
    result = await score_all_companies_smart_money(db)
    log.info("smart_money_scoring_complete", **result)
    return result


async def step_degradation_monitor(db):
    """Run degradation monitoring for all companies."""
    from india_alpha.signals.degradation_monitor import monitor_all_companies
    log.info("═══ STEP 13: DEGRADATION MONITORING ═══")
    result = await monitor_all_companies(db)
    log.info("degradation_monitoring_complete", **result)
    return result


async def step_composite_score(db, settings, cost_tracker=None):
    """Composite hidden gem scoring."""
    from india_alpha.processing.gem_scorer import run_full_scoring
    log.info("═══ STEP 14: COMPOSITE GEM SCORING ═══")

    claude_client = None
    if settings.anthropic_api_key:
        import anthropic
        claude_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    result = await run_full_scoring(
        db, claude_client, settings.claude_model, cost_tracker=cost_tracker
    )
    log.info("composite_scoring_complete", **result)
    return result


async def step_output(db):
    """Print the top hidden gems to console."""
    log.info("═══ STEP 15: TOP HIDDEN GEMS OUTPUT ═══")

    result = await db.table("india_hidden_gems") \
        .select("*") \
        .in_("conviction_tier", ["HIGHEST", "HIGH", "MEDIUM"]) \
        .order("final_score", desc=True) \
        .limit(20) \
        .execute()
    gems = result.data or []

    if not gems:
        log.warning("no_gems_found",
                    msg="No scored gems yet — run earlier pipeline steps first")
        return []

    print(f"\n{'═'*60}")
    print(f"  JIP HORIZON INDIA — TOP HIDDEN GEMS")
    print(f"  Generated: {datetime.now().strftime('%d %b %Y %H:%M IST')}")
    print(f"{'═'*60}")

    for gem in gems:
        p_score = gem.get("promoter_score") or 0
        o_score = gem.get("operating_leverage_score") or 0
        c_score = gem.get("concall_score") or 0
        pol_score = gem.get("policy_tailwind_score") or 0
        q_score = gem.get("quality_emergence_score") or 0

        print(f"\n{'─'*60}")
        print(f"  {gem['company_name']} ({gem['ticker']}) | {gem['conviction_tier']}")
        print(f"  ₹{gem.get('market_cap_cr') or '?'} Cr | "
              f"{gem.get('analyst_count') or 0} analysts | "
              f"Layers firing: {gem.get('layers_firing', 0)}")
        base = gem.get("base_composite") or "—"
        final = gem.get("final_score") or "—"
        print(f"  Promoter: {p_score}  |  OL: {o_score}  |  Corp Intel: {c_score}  |  "
              f"Policy: {pol_score}  |  Quality: {q_score}")

        val_mult = gem.get("valuation_multiplier") or 1.0
        sm_bonus = int(gem.get("smart_money_bonus") or 0)
        deg_penalty = int(gem.get("degradation_penalty") or 0)
        is_deg = gem.get("is_degrading", False)
        deg_flag = " DEGRADING" if is_deg else ""
        print(f"  Base: {base}  →  x{val_mult}  {sm_bonus:+d}sm  {deg_penalty}deg  →  Final: {final}")
        print(f"  Degradation: {deg_penalty}{deg_flag}")

        if gem.get("gem_thesis"):
            print(f"\n  THESIS: {gem['gem_thesis']}")

        if gem.get("key_catalyst"):
            print(f"  CATALYST: {gem['key_catalyst']} ({gem.get('catalyst_timeline', '?')})")

        if gem.get("what_market_misses"):
            print(f"  MARKET MISSES: {gem['what_market_misses']}")

    print(f"\n{'═'*60}")
    print(f"  Total gems scored: {len(gems)}")
    high_conv = sum(1 for g in gems if g.get("conviction_tier") in ("HIGHEST", "HIGH"))
    print(f"  HIGH/HIGHEST conviction: {high_conv}")
    print(f"{'═'*60}\n")

    return gems


async def step_verify(db):
    """Verification dashboard — checks data coverage across all tables."""
    from india_alpha.db import fetch_all_rows
    log.info("═══ VERIFICATION DASHBOARD ═══")

    # Get total universe size
    all_companies = await fetch_all_rows(db, "india_companies", select="ticker")
    total_companies = len(all_companies)

    # Table checks
    tables = [
        ("india_companies", "ticker", "Universe"),
        ("india_financials_history", "isin", "Financials"),
        ("india_promoter_signals", "isin", "Insider Signals"),
        ("india_promoter_summary", "isin", "Promoter Scores"),
        ("india_corporate_filings", "ticker", "Corp Filings"),
        ("india_corporate_intelligence_scores", "isin", "Corp Intel Scores"),
        ("india_operating_leverage_scores", "isin", "OL Scores"),
        ("india_quality_scores", "isin", "Quality Scores"),
        ("india_policy_scores", "isin", "Policy Scores"),
        ("india_valuation_scores", "isin", "Valuation Scores"),
        ("india_shareholding_patterns", "isin", "Shareholding"),
        ("india_bulk_deals", "ticker", "Bulk Deals"),
        ("india_smart_money_scores", "isin", "Smart Money Scores"),
        ("india_degradation_flags", "isin", "Degradation"),
        ("india_hidden_gems", "isin", "Hidden Gems"),
    ]

    print(f"\n{'═'*70}")
    print(f"  JIP HORIZON INDIA — DATA VERIFICATION DASHBOARD")
    print(f"  {datetime.now().strftime('%d %b %Y %H:%M IST')}")
    print(f"  Total Universe: {total_companies:,} companies")
    print(f"{'═'*70}")
    print(f"  {'Table':<30} {'Rows':>8} {'Unique':>8} {'Coverage':>10}")
    print(f"  {'─'*66}")

    warnings = []

    for table_name, unique_col, label in tables:
        try:
            rows = await fetch_all_rows(db, table_name, select=unique_col)
            total_rows = len(rows)
            unique_vals = len({r[unique_col] for r in rows if r.get(unique_col)})
            coverage_pct = (unique_vals / total_companies * 100) if total_companies > 0 else 0

            status = ""
            if table_name == "india_companies":
                status = ""  # Reference table
            elif coverage_pct >= 70:
                status = "OK"
            elif coverage_pct >= 30:
                status = "PARTIAL"
                warnings.append(f"{label}: {coverage_pct:.0f}% coverage")
            elif total_rows > 0:
                status = "LOW"
                warnings.append(f"{label}: {coverage_pct:.0f}% coverage ({total_rows} rows)")
            else:
                status = "EMPTY"
                warnings.append(f"{label}: NO DATA")

            print(f"  {label:<30} {total_rows:>8,} {unique_vals:>8,} {coverage_pct:>8.1f}%  {status}")
        except Exception as exc:
            print(f"  {label:<30} {'ERROR':>8} {'—':>8} {'—':>10}")
            warnings.append(f"{label}: query failed ({str(exc)[:50]})")

    print(f"  {'─'*66}")

    if warnings:
        print(f"\n  WARNINGS:")
        for w in warnings:
            print(f"    - {w}")
    else:
        print(f"\n  All tables have >70% coverage!")

    print(f"{'═'*70}\n")

    return {"total_companies": total_companies, "warnings": warnings}


async def run_full_pipeline(args):
    """Run all steps in sequence."""
    from india_alpha.config import get_settings
    from india_alpha.db import get_async_db
    from india_alpha.cost_tracker import CostTracker

    settings = get_settings()
    db = await get_async_db()

    # Shared cost tracker across all Claude-using steps
    cost_tracker = CostTracker(daily_budget_usd=settings.claude_daily_budget_usd)

    start = datetime.now()
    log.info("pipeline_start", time=start.isoformat(), step=args.step)

    # Verify step doesn't need job tracking
    if args.step == "verify":
        await step_verify(db)
        return {}

    # Log job start
    try:
        job_result = await db.table("india_job_runs").insert({
            "job_name": f"pipeline_{args.step}",
            "status": "running",
        }).execute()
        job_id = job_result.data[0]["id"] if job_result.data else None
    except Exception as exc:
        log.warning("job_run_insert_failed", error=str(exc)[:100])
        job_id = None

    results = {}
    try:
        if args.step in ("all", "universe"):
            results["universe"] = await step_universe(
                db, settings, max_symbols=args.max_symbols
            )

        if args.step in ("all", "universe", "enrich"):
            results["screener_enrich"] = await step_screener_enrich(db, settings)

        if args.step in ("all", "insider"):
            results["insider"] = await step_insider(db, days_back=args.days_back)

        if args.step in ("all", "insider", "score"):
            results["promoter_score"] = await step_promoter_score(db)

        if args.step in ("all", "financials"):
            results["financials"] = await step_financials(
                db, settings, max_companies=args.max_companies
            )

        if args.step in ("all", "financials", "score"):
            results["ol_score"] = await step_ol_score(db)

        if args.step in ("all", "quality", "score"):
            results["quality_score"] = await step_quality_score(db)

        if args.step in ("all", "policy", "score"):
            results["policy_score"] = await step_policy_score(db)

        if args.step in ("all", "corporate"):
            results["corporate_fetch"] = await step_corporate_filings_fetch(
                db, settings, max_companies=args.max_companies,
                fetch_all_companies=args.fetch_all,
            )
            results["corporate_score"] = await step_corporate_intelligence_score(
                db, settings, cost_tracker
            )

        if args.step in ("all", "valuation", "score"):
            results["valuation"] = await step_valuation_score(db)

        if args.step in ("all", "smartmoney"):
            results["smart_money_fetch"] = await step_smart_money_fetch(db, settings)
            results["smart_money_score"] = await step_smart_money_score(db)

        if args.step in ("all", "score"):
            results["smart_money_score"] = await step_smart_money_score(db)

        if args.step in ("all", "degradation", "score"):
            results["degradation"] = await step_degradation_monitor(db)

        if args.step in ("all", "score"):
            results["composite"] = await step_composite_score(
                db, settings, cost_tracker
            )

        if args.step in ("all", "output"):
            results["output"] = await step_output(db)

        elapsed = (datetime.now() - start).total_seconds()
        log.info("pipeline_complete",
                 elapsed_seconds=round(elapsed, 1),
                 steps_run=list(results.keys()))

        # Update job status
        if job_id:
            await db.table("india_job_runs").update({
                "status": "success",
                "completed_at": datetime.now().isoformat(),
                "details": results,
            }).eq("id", job_id).execute()

    except Exception as e:
        log.error("pipeline_failed", error=str(e))
        if job_id:
            await db.table("india_job_runs").update({
                "status": "failed",
                "completed_at": datetime.now().isoformat(),
                "error_msg": str(e)[:500],
            }).eq("id", job_id).execute()
        raise

    return results


def main():
    parser = argparse.ArgumentParser(description="JIP Horizon India Pipeline")
    parser.add_argument(
        "--step",
        choices=["all", "universe", "enrich", "insider", "financials", "quality",
                 "policy", "corporate", "valuation", "smartmoney",
                 "degradation", "score", "output", "verify"],
        default="all",
        help="Which step to run"
    )
    parser.add_argument("--max-symbols", type=int, default=2000,
                        help="Max NSE symbols to fetch (default 2000)")
    parser.add_argument("--max-companies", type=int, default=2000,
                        help="Max companies for Screener/corporate filings fetch (default 2000)")
    parser.add_argument("--days-back", type=int, default=365,
                        help="Days back for insider fetch (default 365)")
    parser.add_argument("--fetch-all", action="store_true", default=False,
                        help="Fetch data for ALL companies (bypass score filters)")
    args = parser.parse_args()

    asyncio.run(run_full_pipeline(args))


if __name__ == "__main__":
    main()
