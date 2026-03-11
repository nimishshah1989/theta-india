# JIP Horizon India — Project Architecture

## What This Is
Quantitative hidden gem stock screener for Indian mid/small-cap markets.
Identifies undervalued, undiscovered companies using 5 signal layers + 3 modifiers.

## Tech Stack
- **Runtime**: Python 3.11+ with async/await
- **Framework**: FastAPI (API routes not yet built)
- **Database**: Supabase (PostgreSQL) — 16 tables prefixed `india_`
- **AI**: Anthropic Claude (thesis synthesis + corporate filing analysis, cost-controlled)
- **Data Sources**: NSE CSV, NSE Corporate Announcements API, NSE Bulk Deals API, BSE API, BSE Shareholding API, Screener.in, yfinance

## Architecture
```
Fetchers (data ingestion) → Signals (pure Python scoring) → Modifiers → Processing (composite + Claude) → Output

Base composite = weighted_average(rescaled(promoter):30%, rescaled(OL):30%, rescaled(corp_intel):25%, rescaled(policy):10%, rescaled(quality):5%)
+ convergence bonus (6%/10%/15% for 2/3/4+ layers >= 40 rescaled)

Modifiers:
  valuation_multiplier = 0.75x to 1.15x  (multiplicative gate)
  smart_money_bonus    = -10 to +15       (additive confirmation)
  degradation_penalty  = -30 to 0         (subtractive red flags)

  final = clamp(base * valuation_mult + smart_money + degradation, 0, 100)
```

### Signal Layers (5 weighted)
| Layer | Weight | Status |
|-------|--------|--------|
| Promoter insider trading | 30% | ACTIVE |
| Operating leverage inflection | 30% | ACTIVE |
| Corporate intelligence | 25% | ACTIVE (NSE filings + Python rules + Claude) |
| Policy tailwind | 10% | ACTIVE (policy_registry.json) |
| Quality emergence | 5% | ACTIVE (pure Python) |

### Modifiers (3, applied after base composite)
| Modifier | Range | Type | Status |
|----------|-------|------|--------|
| Valuation gate | 0.75x–1.15x | Multiplicative | ACTIVE (yfinance data, pure Python) |
| Smart money bonus | -10 to +15 | Additive | ACTIVE (NSE bulk deals + BSE shareholding) |
| Degradation penalty | -30 to 0 | Subtractive | ACTIVE (cross-table scan, pure Python) |

### Conviction Tiers
- HIGHEST: composite >= 70
- HIGH: composite >= 58
- MEDIUM: composite >= 45
- WATCH: composite >= 30

## Pipeline Steps (15 total)
| Step | Name | Type | Data Source | Cost |
|------|------|------|-------------|------|
| 1 | Universe build | Fetcher | NSE CSV + yfinance (incl. valuation fields) | Free |
| 2 | BSE insider signals | Fetcher | BSE API | Free |
| 3 | Promoter scoring | Scorer | india_promoter_signals | Free |
| 4 | Screener.in financials | Fetcher | Screener.in | Free |
| 5 | OL scoring | Scorer | india_financials_history | Free |
| 6 | Quality scoring | Scorer | india_financials_history | Free |
| 7 | Policy scoring | Scorer | policy_registry.json | Free |
| 8 | NSE corporate filings | Fetcher | NSE API | Free |
| 9 | Corporate intelligence | Scorer | india_corporate_filings + Claude | ~$0.02/filing |
| 10 | Valuation scoring | Scorer | india_companies (yfinance data) | Free |
| 11 | Smart money data fetch | Fetcher | NSE bulk deals + BSE shareholding | Free |
| 12 | Smart money scoring | Scorer | india_bulk_deals + india_shareholding_patterns | Free |
| 13 | Degradation monitoring | Scorer | All existing tables (cross-table scan) | Free |
| 14 | Composite gem scoring | Processing | All score tables | ~$0.005/thesis |
| 15 | Output | Display | india_hidden_gems | Free |

## Key Files
- `scripts/run_pipeline.py` — Main CLI orchestrator (15 steps)
- `scripts/test_connection.py` — System health check
- `india_alpha/db.py` — Supabase client (async + sync)
- `india_alpha/config.py` — Pydantic settings from .env
- `india_alpha/cost_tracker.py` — Claude API cost tracking
- `india_alpha/fetchers/` — Data ingestion modules
  - `universe_builder.py` — NSE symbol universe + yfinance valuation fields
  - `bse_insider.py` — BSE insider trading signals (fallback)
  - `nse_insider_fetcher.py` — NSE PIT disclosures (primary)
  - `screener_fetcher.py` — Screener.in financials + shareholding
  - `screener_enricher.py` — Screener.in company enrichment (valuation data)
  - `nse_filings_fetcher.py` — NSE corporate announcements + PDF extraction
  - `nse_bulk_deals_fetcher.py` — NSE bulk/block deal data
  - `bse_shareholding_fetcher.py` — BSE quarterly shareholding patterns (fallback)
  - `nse_shareholding_fetcher.py` — NSE shareholding patterns (primary)
- `india_alpha/signals/` — Scoring modules
  - `promoter_scorer.py` — Layer 1: Insider trading scoring
  - `operating_leverage.py` — Layer 2: OL inflection scoring
  - `corporate_intelligence_scorer.py` — Layer 3: Python rules + Claude filing analysis
  - `policy_scorer.py` — Layer 4: Policy tailwind scoring
  - `quality_scorer.py` — Layer 5: Quality emergence scoring
  - `valuation_scorer.py` — Modifier: Valuation gate (PE, P/B, EV/EBITDA, 52-week)
  - `smart_money_scorer.py` — Modifier: Smart money bonus (superstar + MF/FII + bulk deals)
  - `degradation_monitor.py` — Modifier: Degradation penalty (10 red flags)
- `india_alpha/data/policy_registry.json` — Government policy-sector mapping
- `india_alpha/data/superstar_investors.json` — 25 Indian superstar investors + institutional keywords
- `india_alpha/processing/gem_scorer.py` — Composite scoring + modifiers + Claude thesis
- `schema.sql` — Run in Supabase SQL editor to create tables

## Running
```bash
pip install -r requirements.txt
python scripts/test_connection.py          # Verify setup
python scripts/run_pipeline.py --step all --max-symbols 50  # Quick test
python scripts/run_pipeline.py --step all  # Full run (300 symbols)
python scripts/run_pipeline.py --step quality   # Quality only
python scripts/run_pipeline.py --step policy    # Policy only
python scripts/run_pipeline.py --step corporate --max-companies 3  # Corporate filings only
python scripts/run_pipeline.py --step valuation     # Valuation scoring only
python scripts/run_pipeline.py --step smartmoney    # Smart money fetch + score
python scripts/run_pipeline.py --step degradation   # Degradation monitoring only
python scripts/run_pipeline.py --step score     # Rescore all layers + modifiers + composite
```

## Environment Variables
See `.env.example` — requires SUPABASE_URL, SUPABASE_KEY, ANTHROPIC_API_KEY
Optional: SCREENER_SESSION_COOKIE (for Screener.in financials)

## Coding Rules
- All DB calls use async Supabase client (`get_async_db()`)
- Signal scorers are pure Python (no API cost, testable) except corporate intelligence (Claude for top filings)
- All 3 modifiers are pure Python — $0 additional cost
- Claude used for: HIGH/HIGHEST tier thesis (~$0.005/call) + top corporate filing analysis (~$0.02/filing, max 40/run)
- Indian currency: store as float in crores, display with Indian formatting
- All timestamps in IST context
