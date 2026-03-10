"""
check_coverage.py
Quick diagnostic: counts rows in every key signal table and shows score distributions.
Usage: python scripts/check_coverage.py
"""

import sys
import os

# Add project root to path (same pattern as run_pipeline.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from india_alpha.db import get_db


def count_rows(db, table: str) -> int:
    """Count total rows in a table using a minimal select with count header."""
    try:
        resp = db.table(table).select("id", count="exact").limit(0).execute()
        return resp.count if resp.count is not None else 0
    except Exception as exc:
        print(f"  [ERROR] Could not query {table}: {exc}")
        return -1


def count_with_score_filter(db, table: str, score_col: str, min_score: int) -> int:
    """Count rows where score_col >= min_score."""
    try:
        resp = (
            db.table(table)
            .select("id", count="exact")
            .gte(score_col, min_score)
            .limit(0)
            .execute()
        )
        return resp.count if resp.count is not None else 0
    except Exception as exc:
        print(f"  [ERROR] Could not filter {table}.{score_col} >= {min_score}: {exc}")
        return -1


def count_conviction_tiers(db) -> dict:
    """Count india_hidden_gems by conviction_tier."""
    tiers = {}
    for tier in ["HIGHEST", "HIGH", "MEDIUM", "WATCH"]:
        try:
            resp = (
                db.table("india_hidden_gems")
                .select("id", count="exact")
                .eq("conviction_tier", tier)
                .limit(0)
                .execute()
            )
            tiers[tier] = resp.count if resp.count is not None else 0
        except Exception as exc:
            tiers[tier] = -1
    return tiers


def main():
    print("=" * 72)
    print("  JIP HORIZON INDIA -- Data Coverage Diagnostic")
    print("=" * 72)
    print()

    db = get_db()

    # ── Section 1: Raw data tables ──────────────────────────────────────
    raw_tables = [
        ("india_companies", "Universe (master registry)"),
        ("india_promoter_signals", "Raw insider trades (Layer 1)"),
        ("india_financials_history", "Raw financials (Layer 2)"),
        ("india_corporate_filings", "Raw corporate filings (Layer 3 new)"),
        ("india_concalls", "Raw concall transcripts (Layer 3 old/deprecated)"),
    ]

    print("-" * 72)
    print(f"  {'TABLE':<40} {'ROWS':>8}  DESCRIPTION")
    print("-" * 72)

    for table, desc in raw_tables:
        count = count_rows(db, table)
        count_str = f"{count:,}" if count >= 0 else "ERROR"
        print(f"  {table:<40} {count_str:>8}  {desc}")

    print()

    # ── Section 2: Scored tables with score distribution ────────────────
    scored_tables = [
        ("india_promoter_summary", "promoter_signal_score", "Layer 1 - Promoter Signal"),
        ("india_operating_leverage_scores", "ol_score", "Layer 2 - Operating Leverage"),
        ("india_corporate_intelligence_scores", "corporate_intelligence_score", "Layer 3 - Corporate Intel (new)"),
        ("india_concall_signals", "concall_signal_score", "Layer 3 - Concall (old/deprecated)"),
        ("india_policy_scores", "policy_score", "Layer 4 - Policy Tailwind"),
        ("india_quality_scores", "quality_score", "Layer 5 - Quality Emergence"),
    ]

    print("-" * 72)
    print(f"  {'SCORED TABLE':<40} {'TOTAL':>7} {'> 0':>7} {'>= 25':>7}  LAYER")
    print("-" * 72)

    for table, score_col, layer_name in scored_tables:
        total = count_rows(db, table)
        gt_zero = count_with_score_filter(db, table, score_col, 1)
        gte_25 = count_with_score_filter(db, table, score_col, 25)

        total_str = f"{total:,}" if total >= 0 else "ERR"
        gt0_str = f"{gt_zero:,}" if gt_zero >= 0 else "ERR"
        g25_str = f"{gte_25:,}" if gte_25 >= 0 else "ERR"

        print(f"  {table:<40} {total_str:>7} {gt0_str:>7} {g25_str:>7}  {layer_name}")

    print()

    # ── Section 3: Final output table ───────────────────────────────────
    gems_total = count_rows(db, "india_hidden_gems")
    tiers = count_conviction_tiers(db)

    print("-" * 72)
    print("  INDIA_HIDDEN_GEMS (Final Output)")
    print("-" * 72)
    gems_str = f"{gems_total:,}" if gems_total >= 0 else "ERROR"
    print(f"  Total gems scored:  {gems_str}")
    print()
    print(f"    HIGHEST conviction:  {tiers.get('HIGHEST', '?'):>6}")
    print(f"    HIGH conviction:     {tiers.get('HIGH', '?'):>6}")
    print(f"    MEDIUM conviction:   {tiers.get('MEDIUM', '?'):>6}")
    print(f"    WATCH conviction:    {tiers.get('WATCH', '?'):>6}")

    # Check for gems with no tier (below WATCH threshold)
    tiered_total = sum(v for v in tiers.values() if isinstance(v, int) and v >= 0)
    if gems_total > tiered_total:
        print(f"    Below WATCH / NULL:  {gems_total - tiered_total:>6}")

    print()
    print("=" * 72)
    print("  Done.")
    print("=" * 72)


if __name__ == "__main__":
    main()
