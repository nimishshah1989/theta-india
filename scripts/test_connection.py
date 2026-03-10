"""
test_connection.py
Quick smoke test — verifies DB connection and each module imports cleanly.
Run this FIRST before anything else.

Usage: python scripts/test_connection.py
"""

import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structlog
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True)
    ]
)
log = structlog.get_logger()


async def test_db_connection():
    """Test Supabase connection."""
    print("\n1. Testing Supabase connection...")
    try:
        from india_alpha.config import get_settings
        from india_alpha.db import get_async_db

        settings = get_settings()
        print(f"   SUPABASE_URL: {settings.supabase_url[:40]}...")
        print(f"   ANTHROPIC_KEY: {'✅ set' if settings.anthropic_api_key else '❌ NOT SET'}")
        print(f"   SCREENER_COOKIE: {'✅ set' if settings.screener_session_cookie else '⚠️  not set (optional)'}")

        db = await get_async_db()

        # Try a simple query
        result = await db.table("india_companies").select("count").execute()
        print(f"   ✅ DB connected!")

        # Check which tables exist
        tables_to_check = [
            "india_companies", "india_promoter_signals", "india_promoter_summary",
            "india_financials_history", "india_operating_leverage_scores",
            "india_hidden_gems", "india_job_runs"
        ]

        print("\n   Checking tables:")
        for table in tables_to_check:
            try:
                r = await db.table(table).select("id").limit(1).execute()
                print(f"   ✅ {table}")
            except Exception as e:
                print(f"   ❌ {table} — {str(e)[:60]}")
                print(f"      → Run schema.sql in Supabase SQL editor first!")

        return True

    except Exception as e:
        print(f"   ❌ Connection failed: {e}")
        print("\n   → Check your .env file has SUPABASE_URL and SUPABASE_KEY")
        return False


async def test_modules():
    """Test all module imports."""
    print("\n2. Testing module imports...")
    modules = [
        ("universe_builder", "india_alpha.fetchers.universe_builder"),
        ("bse_insider", "india_alpha.fetchers.bse_insider"),
        ("screener_fetcher", "india_alpha.fetchers.screener_fetcher"),
        ("promoter_scorer", "india_alpha.signals.promoter_scorer"),
        ("operating_leverage", "india_alpha.signals.operating_leverage"),
        ("gem_scorer", "india_alpha.processing.gem_scorer"),
    ]

    all_ok = True
    for name, module_path in modules:
        try:
            __import__(module_path)
            print(f"   ✅ {name}")
        except Exception as e:
            print(f"   ❌ {name}: {e}")
            all_ok = False

    return all_ok


async def test_promoter_scorer_logic():
    """Test promoter scorer with synthetic data (no DB needed)."""
    print("\n3. Testing promoter scorer logic...")
    from india_alpha.signals.promoter_scorer import compute_promoter_score

    # Synthetic: strong promoter buying
    signals = [
        {"signal_type": "open_market_buy", "value_cr": 3.5,
         "transaction_date": "2026-02-15", "person_name": "Ravi Sharma"},
        {"signal_type": "open_market_buy", "value_cr": 2.1,
         "transaction_date": "2026-01-20", "person_name": "Ravi Sharma"},
        {"signal_type": "pledge_decrease", "pledge_pct_after": 5,
         "transaction_date": "2026-01-10"},
    ]
    score = compute_promoter_score(signals)
    print(f"   Score: {score['promoter_signal_score']}/100")
    print(f"   Narrative: {score['score_narrative']}")
    assert score["promoter_signal_score"] > 20, "Expected score > 20 for strong buying"
    print(f"   ✅ Promoter scorer logic working")

    # Synthetic: negative signals
    bad_signals = [
        {"signal_type": "open_market_sell", "value_cr": 8.0,
         "transaction_date": "2026-02-10"},
        {"signal_type": "pledge_increase", "value_cr": 0,
         "transaction_date": "2026-01-05"},
    ]
    bad_score = compute_promoter_score(bad_signals)
    print(f"   Negative signal score: {bad_score['promoter_signal_score']}/100")
    assert bad_score["promoter_signal_score"] < 20, "Expected score < 20 for selling + pledge"
    print(f"   ✅ Negative signals correctly penalised")


async def test_ol_scorer_logic():
    """Test OL scorer with synthetic financial data."""
    print("\n4. Testing operating leverage scorer...")
    from india_alpha.signals.operating_leverage import compute_ol_score

    # Synthetic: strong inflection
    history = [
        {  # Current year
            "revenue_cr": 450, "ebitda_margin_pct": 18,
            "total_debt_cr": 0, "cash_cr": 35,
            "roce": 22, "debtor_days": 45, "net_worth_cr": 180
        },
        {  # 1 year ago
            "revenue_cr": 320, "ebitda_margin_pct": 14,
            "total_debt_cr": 85, "cash_cr": 12,
            "roce": 15, "debtor_days": 58, "net_worth_cr": 130
        },
        {  # 2 years ago
            "revenue_cr": 240, "ebitda_margin_pct": 12,
            "total_debt_cr": 110, "cash_cr": 8,
            "roce": 11, "debtor_days": 65, "net_worth_cr": 95
        },
    ]

    score = compute_ol_score(history)
    print(f"   OL Score: {score['ol_score']}/100")
    print(f"   Signals firing: {score['signals_firing']}")
    print(f"   Is inflection candidate: {score['is_inflection_candidate']}")
    print(f"   Narrative: {score['score_narrative']}")
    assert score["ol_score"] > 40, "Expected strong OL score for this data"
    print(f"   ✅ OL scorer logic working")


async def quick_insider_test():
    """Test BSE insider fetch (just the API, don't store)."""
    print("\n5. Testing BSE insider fetch (dry run)...")
    from india_alpha.fetchers.bse_insider import fetch_from_insiderscreener
    try:
        records = await fetch_from_insiderscreener()
        print(f"   Fetched {len(records)} records from insiderscreener.com")
        if records:
            sample = records[0]
            print(f"   Sample: {sample.get('company', sample.get('Company_Name', 'Unknown'))}")
        print(f"   ✅ BSE insider fetch working")
        return True
    except Exception as e:
        print(f"   ⚠️  Insider fetch: {str(e)[:80]}")
        print(f"   (This might be network access — check if insiderscreener.com is reachable)")
        return False


async def main():
    print("━" * 50)
    print("  JIP HORIZON INDIA — System Check")
    print("━" * 50)

    db_ok = await test_db_connection()
    mod_ok = await test_modules()

    if mod_ok:
        await test_promoter_scorer_logic()
        await test_ol_scorer_logic()

    await quick_insider_test()

    print("\n" + "━" * 50)
    if db_ok and mod_ok:
        print("  ✅ ALL SYSTEMS GO — Ready to run pipeline")
        print("\n  Next step:")
        print("  python scripts/run_pipeline.py --step all --max-symbols 50")
        print("  (Start with 50 symbols to verify the full flow quickly)")
    else:
        print("  ❌ Fix errors above before running pipeline")
        if not db_ok:
            print("  → Create .env with SUPABASE_URL + SUPABASE_KEY")
            print("  → Run schema.sql in Supabase SQL editor")
    print("━" * 50)


if __name__ == "__main__":
    asyncio.run(main())
