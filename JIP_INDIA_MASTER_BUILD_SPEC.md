# JIP Horizon India — Master Build Specification
**Version:** 1.0 | **Date:** March 2026 | **Author:** Nimish Jhaveri / JIP Platform  
**Status:** Active Build Document — This is the single source of truth.

---

## CRITICAL INSTRUCTIONS FOR CLAUDE CODE

> Read this entire document before writing a single line of code.  
> Do not improvise formulas. Do not invent scoring logic. Do not add tables not listed here.  
> If something is unclear, ask before building. The system has a defined spec — follow it exactly.  
> When in doubt, refer back to the relevant section of this document.

---

## Table of Contents

1. [What This System Is](#1-what-this-system-is)
2. [Repository Structure](#2-repository-structure)
3. [Environment and Configuration](#3-environment-and-configuration)
4. [Database Schema — Complete](#4-database-schema--complete)
5. [Pipeline Steps and Execution Order](#5-pipeline-steps-and-execution-order)
6. [Data Sources](#6-data-sources)
7. [Layer 1: Promoter Insider Trading (30%)](#7-layer-1-promoter-insider-trading-30)
8. [Layer 2: Operating Leverage Inflection (30%)](#8-layer-2-operating-leverage-inflection-30)
9. [Layer 3: Corporate Intelligence (25%)](#9-layer-3-corporate-intelligence-25)
10. [Layer 4: Policy Tailwind (10%)](#10-layer-4-policy-tailwind-10)
11. [Layer 5: Quality Emergence (5%)](#11-layer-5-quality-emergence-5)
12. [Modifier 1: Valuation Gate (×0.75 to ×1.15)](#12-modifier-1-valuation-gate)
13. [Modifier 2: Smart Money Bonus (−10 to +15)](#13-modifier-2-smart-money-bonus)
14. [Modifier 3: Degradation Penalty (−30 to 0)](#14-modifier-3-degradation-penalty)
15. [Composite Scoring — The Final Formula](#15-composite-scoring--the-final-formula)
16. [Claude Thesis Synthesis](#16-claude-thesis-synthesis)
17. [Build Status: What Exists vs What's Missing](#17-build-status-what-exists-vs-whats-missing)
18. [Next Build Session — Exact Tasks](#18-next-build-session--exact-tasks)

---

## 1. What This System Is

**JIP Horizon India** is a quantitative hidden gem stock screener for Indian mid/small-cap equities. It finds undiscovered companies — below institutional radar, low analyst coverage — where multiple quantitative signals are converging before the market notices.

**Core thesis:** When a promoter is buying shares on the open market with personal capital AND the company's operating leverage is inflecting AND it aligns with a government policy tailwind AND the stock is cheap, that is a high-conviction opportunity that almost no fund manager has modeled.

**What it is NOT:**
- Not a trading system
- Not a concall summariser
- Not a general-purpose financial dashboard
- Not a replacement for human judgment — it surfaces candidates, not decisions

**Outputs one thing:** A ranked list of Indian companies with a composite conviction score (0–100) and tier classification (HIGHEST / HIGH / MEDIUM / WATCH), with a Claude-generated 3-sentence thesis for the top tier.

**Cost target:** Under $1.00 per full pipeline run for ~500 companies.

**Infrastructure:**
- Language: Python 3.11+
- Database: Supabase (PostgreSQL) — same project as jip-horizon global engine
- Tables: All prefixed `india_` to coexist with global tables
- Port: 8001 (global engine runs on 8000)
- Deployment: AWS EC2 or Railway (same as global engine)

---

## 2. Repository Structure

```
jip-horizon-india/
├── india_alpha/
│   ├── __init__.py
│   ├── config.py                          # Pydantic Settings, reads .env
│   ├── db.py                              # Supabase singleton (cached)
│   ├── fetchers/
│   │   ├── __init__.py
│   │   ├── universe_builder.py            # NSE symbol list + yfinance enrichment
│   │   ├── bse_insider.py                 # BSE PIT disclosure fetcher
│   │   ├── screener_fetcher.py            # Screener.in financial data fetcher
│   │   ├── nse_filings_fetcher.py         # NSE corporate announcements fetcher  [MISSING]
│   │   ├── nse_bulk_deals_fetcher.py      # NSE bulk/block deals fetcher          [MISSING]
│   │   └── shareholding_fetcher.py        # BSE quarterly shareholding patterns   [MISSING]
│   ├── signals/
│   │   ├── __init__.py
│   │   ├── promoter_scorer.py             # Layer 1 — promoter signal scoring      [BUILT]
│   │   ├── operating_leverage.py          # Layer 2 — OL inflection scoring        [BUILT]
│   │   ├── corporate_intelligence.py      # Layer 3 — NSE filings + Claude         [MISSING]
│   │   ├── policy_scorer.py               # Layer 4 — policy tailwind scoring      [BUILT]
│   │   ├── quality_scorer.py              # Layer 5 — quality emergence scoring    [BUILT]
│   │   ├── valuation_scorer.py            # Modifier 1 — valuation gate            [BUILT]
│   │   ├── smart_money_scorer.py          # Modifier 2 — smart money bonus         [MISSING]
│   │   └── degradation_monitor.py        # Modifier 3 — degradation penalty       [BUILT]
│   ├── processing/
│   │   ├── __init__.py
│   │   └── gem_scorer.py                  # Composite scoring + Claude thesis      [BUILT]
│   ├── api/
│   │   ├── __init__.py
│   │   └── routes.py                      # FastAPI endpoints                      [MISSING]
│   └── data/
│       ├── __init__.py
│       └── policy_registry.json           # 15 active government schemes           [BUILT]
├── scripts/
│   ├── run_pipeline.py                    # Master pipeline runner                 [BUILT]
│   └── test_connection.py                 # DB + module smoke test                 [BUILT]
├── schema.sql                             # Complete Supabase schema (run once)    [BUILT]
├── requirements.txt
├── .env.example
└── main.py                                # FastAPI app entry point                [MISSING]
```

**Do not create any files not listed above. Do not rename files. Do not reorganize folders.**

---

## 3. Environment and Configuration

### `.env` file (required before running anything)

```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-service-role-key
ANTHROPIC_API_KEY=sk-ant-...
SCREENER_SESSION_COOKIE=your-screener-sessionid-cookie
ENVIRONMENT=development
PORT=8001
```

### `india_alpha/config.py` — exact spec

```python
from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    supabase_url: str = ""
    supabase_key: str = ""
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"
    claude_daily_budget_usd: float = 0.30
    screener_session_cookie: str = ""
    environment: str = "development"
    port: int = 8001

    class Config:
        env_file = ".env"

@lru_cache()
def get_settings() -> Settings:
    return Settings()
```

### `india_alpha/db.py` — exact spec

```python
from supabase import create_client, Client
from functools import lru_cache
from india_alpha.config import get_settings

@lru_cache()
def get_db() -> Client:
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_key)
```

---

## 4. Database Schema — Complete

**Run `schema.sql` once in the Supabase SQL Editor. Never modify table names.**  
All tables use `india_` prefix.

### 4.1 Core Tables

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Master company registry
CREATE TABLE IF NOT EXISTS india_companies (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ticker              TEXT NOT NULL,
    exchange            TEXT NOT NULL DEFAULT 'NSE',
    nse_symbol          TEXT,
    bse_code            TEXT,
    company_name        TEXT NOT NULL,
    isin                TEXT,
    sector              TEXT,
    industry            TEXT,
    market_cap_cr       FLOAT,
    market_cap_tier     TEXT,        -- LARGE | MID | SMALL | MICRO | NANO
    revenue_cr_ttm      FLOAT,
    revenue_growth_yoy  FLOAT,
    ebitda_margin       FLOAT,
    pat_cr_ttm          FLOAT,
    roe                 FLOAT,
    debt_equity         FLOAT,
    analyst_count       INTEGER DEFAULT 0,
    is_f_and_o          BOOLEAN DEFAULT false,
    is_index_stock      BOOLEAN DEFAULT false,
    -- Valuation fields (yfinance)
    trailing_pe         FLOAT,
    pb_ratio            FLOAT,
    ev_ebitda           FLOAT,
    current_price       FLOAT,
    fifty_two_week_low  FLOAT,
    fifty_two_week_high FLOAT,
    dma_200             FLOAT,
    valuation_zone      TEXT,
    first_seen          DATE,
    last_updated        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(exchange, ticker)
);
```

### 4.2 Layer 1 Tables

```sql
CREATE TABLE IF NOT EXISTS india_promoter_signals (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                    TEXT NOT NULL,
    ticker                  TEXT NOT NULL,
    company_name            TEXT,
    signal_type             TEXT NOT NULL,
    transaction_date        DATE NOT NULL,
    intimation_date         DATE,
    person_name             TEXT,
    person_category         TEXT,
    transaction_type        TEXT,
    shares                  BIGINT,
    price_per_share         FLOAT,
    value_cr                FLOAT,
    post_transaction_pct    FLOAT,
    pledged_shares_before   BIGINT,
    pledged_shares_after    BIGINT,
    pledge_pct_before       FLOAT,
    pledge_pct_after        FLOAT,
    signal_strength         INTEGER DEFAULT 0,
    source_url              TEXT,
    raw_data                JSONB,
    fetched_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS india_promoter_summary (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                        TEXT UNIQUE NOT NULL,
    ticker                      TEXT NOT NULL,
    open_market_buying_cr_12m   FLOAT DEFAULT 0,
    open_market_selling_cr_12m  FLOAT DEFAULT 0,
    net_buying_cr_12m           FLOAT DEFAULT 0,
    buy_transaction_count_12m   INTEGER DEFAULT 0,
    pledge_trend                TEXT,
    warrant_issued_12m          BOOLEAN DEFAULT false,
    creeping_acq_active         BOOLEAN DEFAULT false,
    promoter_signal_score       INTEGER DEFAULT 0,
    score_narrative             TEXT,
    highest_conviction_signal   TEXT,
    updated_at                  TIMESTAMPTZ DEFAULT NOW()
);
```

### 4.3 Layer 2 Tables

```sql
CREATE TABLE IF NOT EXISTS india_financials_history (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                    TEXT NOT NULL,
    ticker                  TEXT NOT NULL,
    period_type             TEXT NOT NULL,    -- annual | quarterly
    period_end              DATE NOT NULL,
    revenue_cr              FLOAT,
    revenue_growth_yoy      FLOAT,
    ebitda_cr               FLOAT,
    ebitda_margin_pct       FLOAT,
    pat_cr                  FLOAT,
    pat_margin_pct          FLOAT,
    eps                     FLOAT,
    total_debt_cr           FLOAT,
    cash_cr                 FLOAT,
    net_worth_cr            FLOAT,
    total_assets_cr         FLOAT,
    debtor_days             FLOAT,
    inventory_days          FLOAT,
    creditor_days           FLOAT,
    roce                    FLOAT,
    roe                     FLOAT,
    export_revenue_cr       FLOAT,
    capacity_utilisation_pct FLOAT,
    order_book_cr           FLOAT,
    source                  TEXT DEFAULT 'screener_in',
    fetched_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(isin, period_type, period_end)
);

CREATE TABLE IF NOT EXISTS india_operating_leverage_scores (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                    TEXT UNIQUE NOT NULL,
    ticker                  TEXT NOT NULL,
    ol_score                INTEGER DEFAULT 0,
    signals_firing          INTEGER DEFAULT 0,
    is_inflection_candidate BOOLEAN DEFAULT false,
    active_signals          JSONB DEFAULT '[]',
    score_narrative         TEXT,
    scored_at               TIMESTAMPTZ DEFAULT NOW()
);
```

### 4.4 Layer 3 Tables

```sql
CREATE TABLE IF NOT EXISTS india_corporate_filings (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    company_name        TEXT,
    category            TEXT,
    category_bucket     TEXT,    -- EARNINGS_STRATEGY | CAPITAL_ACTION | GOVERNANCE
    subject             TEXT,
    filing_date         DATE,
    filing_url          TEXT,
    extracted_text      TEXT,
    word_count          INTEGER DEFAULT 0,
    is_text_extracted   BOOLEAN DEFAULT false,
    signal_priority     TEXT DEFAULT 'LOW',   -- HIGH | MEDIUM | LOW
    python_score        INTEGER DEFAULT 0,
    claude_score        INTEGER,
    recency_multiplier  FLOAT DEFAULT 1.0,
    final_score         FLOAT DEFAULT 0,
    fetched_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(isin, filing_date, subject)
);

CREATE TABLE IF NOT EXISTS india_corporate_intelligence_scores (
    id                              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                            TEXT UNIQUE NOT NULL,
    ticker                          TEXT NOT NULL,
    earnings_bucket_score           FLOAT DEFAULT 0,
    capital_bucket_score            FLOAT DEFAULT 0,
    governance_bucket_score         FLOAT DEFAULT 0,
    corporate_intelligence_score    INTEGER DEFAULT 0,
    filings_analyzed                INTEGER DEFAULT 0,
    claude_calls_made               INTEGER DEFAULT 0,
    score_narrative                 TEXT,
    scored_at                       TIMESTAMPTZ DEFAULT NOW()
);
```

### 4.5 Layer 4 & 5 Tables

```sql
CREATE TABLE IF NOT EXISTS india_policy_scores (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                    TEXT UNIQUE NOT NULL,
    ticker                  TEXT NOT NULL,
    policy_score            INTEGER DEFAULT 0,
    matched_policies        JSONB DEFAULT '[]',
    policy_count            INTEGER DEFAULT 0,
    highest_impact_policy   TEXT,
    scored_at               TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS india_quality_scores (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin            TEXT UNIQUE NOT NULL,
    ticker          TEXT NOT NULL,
    quality_score   INTEGER DEFAULT 0,
    signals_firing  INTEGER DEFAULT 0,
    active_signals  JSONB DEFAULT '[]',
    score_narrative TEXT,
    scored_at       TIMESTAMPTZ DEFAULT NOW()
);
```

### 4.6 Modifier Tables

```sql
CREATE TABLE IF NOT EXISTS india_valuation_scores (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                    TEXT UNIQUE NOT NULL,
    ticker                  TEXT NOT NULL,
    valuation_score         INTEGER DEFAULT 35,
    valuation_zone          TEXT DEFAULT 'FAIR',
    valuation_multiplier    FLOAT DEFAULT 1.00,
    dimension_scores        JSONB DEFAULT '{}',
    scored_at               TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS india_bulk_deals (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ticker              TEXT NOT NULL,
    isin                TEXT,
    deal_date           DATE NOT NULL,
    client_name         TEXT,
    deal_type           TEXT,        -- BUY | SELL
    quantity            BIGINT,
    price               FLOAT,
    value_cr            FLOAT,
    is_institutional    BOOLEAN DEFAULT false,
    is_superstar        BOOLEAN DEFAULT false,
    superstar_name      TEXT,
    exchange            TEXT DEFAULT 'NSE',
    fetched_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS india_shareholding_patterns (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                    TEXT NOT NULL,
    ticker                  TEXT NOT NULL,
    quarter                 TEXT NOT NULL,    -- Q1FY27, Q2FY27 etc
    promoter_pct            FLOAT,
    fii_pct                 FLOAT,
    dii_pct                 FLOAT,
    mf_pct                  FLOAT,
    public_pct              FLOAT,
    superstar_holdings      JSONB DEFAULT '[]',
    fetched_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(isin, quarter)
);

CREATE TABLE IF NOT EXISTS india_smart_money_scores (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                TEXT UNIQUE NOT NULL,
    ticker              TEXT NOT NULL,
    smart_money_score   INTEGER DEFAULT 0,
    fired_signals       JSONB DEFAULT '[]',
    superstar_entries   TEXT[] DEFAULT '{}',
    superstar_exits     TEXT[] DEFAULT '{}',
    mf_delta_pp         FLOAT DEFAULT 0,
    fii_delta_pp        FLOAT DEFAULT 0,
    scored_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS india_degradation_flags (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                TEXT UNIQUE NOT NULL,
    ticker              TEXT NOT NULL,
    degradation_score   INTEGER DEFAULT 0,
    is_degrading        BOOLEAN DEFAULT false,
    fired_flags         TEXT[] DEFAULT '{}',
    flag_details        JSONB DEFAULT '{}',
    checked_at          TIMESTAMPTZ DEFAULT NOW()
);
```

### 4.7 Output and Monitoring Tables

```sql
CREATE TABLE IF NOT EXISTS india_hidden_gems (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                    TEXT UNIQUE NOT NULL,
    ticker                  TEXT NOT NULL,
    company_name            TEXT NOT NULL,
    exchange                TEXT DEFAULT 'NSE',
    market_cap_cr           FLOAT,
    analyst_count           INTEGER DEFAULT 0,
    -- Raw layer scores
    promoter_score              INTEGER,
    operating_leverage_score    INTEGER,
    concall_score               INTEGER,
    policy_tailwind_score       INTEGER,
    quality_emergence_score     INTEGER,
    -- Modifier values applied
    valuation_multiplier        FLOAT DEFAULT 1.00,
    smart_money_bonus           INTEGER DEFAULT 0,
    degradation_penalty         INTEGER DEFAULT 0,
    -- Composite output
    base_composite          FLOAT,
    final_score             FLOAT,
    conviction_tier         TEXT,
    -- Discovery flags
    is_pre_discovery        BOOLEAN DEFAULT false,
    is_below_institutional  BOOLEAN DEFAULT false,
    layers_firing           INTEGER DEFAULT 0,
    -- Claude thesis (HIGH/HIGHEST only)
    gem_thesis              TEXT,
    key_catalyst            TEXT,
    catalyst_timeline       TEXT,
    catalyst_confidence     TEXT,
    primary_risk            TEXT,
    what_market_misses      TEXT,
    entry_note              TEXT,
    scored_at               TIMESTAMPTZ DEFAULT NOW(),
    last_updated            TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS india_job_runs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_name            TEXT NOT NULL,
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    status              TEXT DEFAULT 'running',
    records_processed   INTEGER DEFAULT 0,
    claude_calls_made   INTEGER DEFAULT 0,
    cost_usd            FLOAT DEFAULT 0,
    error_msg           TEXT,
    details             JSONB DEFAULT '{}'
);
```

### 4.8 Required Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_promoter_signals_isin_date    ON india_promoter_signals(isin, transaction_date DESC);
CREATE INDEX IF NOT EXISTS idx_promoter_signals_type         ON india_promoter_signals(signal_type, transaction_date DESC);
CREATE INDEX IF NOT EXISTS idx_financials_isin_type          ON india_financials_history(isin, period_type, period_end DESC);
CREATE INDEX IF NOT EXISTS idx_corporate_filings_isin        ON india_corporate_filings(isin, filing_date DESC);
CREATE INDEX IF NOT EXISTS idx_bulk_deals_ticker             ON india_bulk_deals(ticker, deal_date DESC);
CREATE INDEX IF NOT EXISTS idx_shareholding_isin             ON india_shareholding_patterns(isin, quarter);
CREATE INDEX IF NOT EXISTS idx_hidden_gems_tier              ON india_hidden_gems(conviction_tier, final_score DESC);
CREATE INDEX IF NOT EXISTS idx_companies_tier                ON india_companies(market_cap_tier, market_cap_cr DESC);
```

---

## 5. Pipeline Steps and Execution Order

These steps run **in this exact order**. Do not reorder.

| Step | Name | File | Input | Output Table | Cost |
|------|------|------|-------|--------------|------|
| 1 | Universe Build | `fetchers/universe_builder.py` | NSE CSV + yfinance | `india_companies` | Free |
| 2 | BSE Insider Fetch | `fetchers/bse_insider.py` | BSE API | `india_promoter_signals` | Free |
| 3 | Promoter Score | `signals/promoter_scorer.py` | `india_promoter_signals` | `india_promoter_summary` | Free |
| 4 | Screener.in Financials | `fetchers/screener_fetcher.py` | Screener.in HTML | `india_financials_history` | Free |
| 5 | OL Score | `signals/operating_leverage.py` | `india_financials_history` | `india_operating_leverage_scores` | Free |
| 6 | Quality Score | `signals/quality_scorer.py` | `india_financials_history` | `india_quality_scores` | Free |
| 7 | Policy Score | `signals/policy_scorer.py` | `india_companies` | `india_policy_scores` | Free |
| 8 | NSE Filings Fetch | `fetchers/nse_filings_fetcher.py` | NSE Announcements API | `india_corporate_filings` | Free |
| 9 | Corp Intelligence Score | `signals/corporate_intelligence.py` | `india_corporate_filings` + Claude | `india_corporate_intelligence_scores` | ~$0.02/filing |
| 10 | Valuation Score | `signals/valuation_scorer.py` | `india_companies` | `india_valuation_scores` | Free |
| 11 | Bulk Deals Fetch | `fetchers/nse_bulk_deals_fetcher.py` | NSE Bulk Deals CSV | `india_bulk_deals` | Free |
| 12 | Shareholding Fetch | `fetchers/shareholding_fetcher.py` | BSE Shareholding API | `india_shareholding_patterns` | Free |
| 13 | Smart Money Score | `signals/smart_money_scorer.py` | `india_bulk_deals` + `india_shareholding_patterns` | `india_smart_money_scores` | Free |
| 14 | Degradation Monitor | `signals/degradation_monitor.py` | All tables | `india_degradation_flags` | Free |
| 15 | Composite Score | `processing/gem_scorer.py` | All score tables | `india_hidden_gems` | ~$0.005/thesis |
| 16 | Output | `scripts/run_pipeline.py` | `india_hidden_gems` | Console / API | Free |

**Pipeline runner:** `scripts/run_pipeline.py --step all`  
Supports `--step universe|insider|financials|score|output` for partial runs.

---

## 6. Data Sources

### 6.1 NSE Symbol Universe
- **URL:** `https://archives.nseindia.com/content/equities/EQUITY_L.csv`
- **Format:** CSV, daily updated
- **Filter:** Series == "EQ" only (excludes warrants, ETFs, debt instruments)
- **Headers:** SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE
- **ISIN column:** Last column (index -1 after split)
- **No auth required**
- **Rate limit:** None (single file download)

### 6.2 yfinance (Company Enrichment)
- **Format:** Python library, ticker = `{NSE_SYMBOL}.NS`
- **Fields used:** marketCap, sector, industry, trailingPE, priceToBook, enterpriseToEbitda, currentPrice, fiftyTwoWeekLow, fiftyTwoWeekHigh, twoHundredDayAverage, returnOnEquity, debtToEquity, totalRevenue, netIncomeToCommon, numberOfAnalystOpinions
- **Rate limit:** ~2,000 requests/day; use 0.5s delay between requests
- **No auth required**
- **Universe filter:** market cap ₹50 Cr to ₹75,000 Cr; exclude Financial Services and Real Estate sectors

### 6.3 BSE Insider Trading (PIT Disclosures)
- **Primary API:** `https://api.bseindia.com/BseIndiaAPI/api/InsiderTrading/w`
  - Params: `fromdate=YYYYMMDD&todate=YYYYMMDD`
  - Requires BSE session cookie (fetch from `https://www.bseindia.com/` first)
  - Returns JSON with `Table` or `Table1` key containing the records
- **Fallback:** `https://www.insiderscreener.com/en/api/india/latest/`
  - No auth for basic access
  - Returns JSON with `data` key
- **Lookback:** 14 days on each run (overlapping fetches deduplicated by isin+date+person+shares)
- **Filed within:** 2 trading days of transaction (SEBI PIT regulation)

### 6.4 Screener.in (Financial History)
- **URL:** `https://www.screener.in/api/company/{TICKER}/standalone/`
  - Fallback: `https://www.screener.in/api/company/{TICKER}/` (consolidated)
- **Auth:** Session cookie — add to request as `Cookie: sessionid={cookie}`
  - Get cookie: Log into screener.in in Chrome → F12 → Application → Cookies → copy `sessionid` value → paste into `.env` as `SCREENER_SESSION_COOKIE`
- **Rate limit:** 1 request per 1.2 seconds (free tier) — strictly respect this
- **Returns:** 10 years annual + 8 quarters of structured financial data
- **403 response** = session cookie expired — user must refresh it

### 6.5 NSE Corporate Announcements
- **Announcements API:** `https://www.nseindia.com/api/corp-info?symbol={SYMBOL}&corpType=announcements`
  - Requires NSE session headers (Referer, User-Agent, Accept)
  - Returns JSON with `data` array
- **Filing categories to capture:** Credit Rating, Dividend, Appointment, Cessation, Resignation, Auditor, Acquisition, Merger, Press Release, Buyback, Bonus, Split, ESOP, Takeover
- **Rate limit:** ~1 request per 1.5 seconds
- **Lookback:** 12 months

### 6.6 NSE Bulk and Block Deals
- **Bulk deals CSV:** `https://www.nseindia.com/api/historical/bulk-deals?from={YYYY-MM-DD}&to={YYYY-MM-DD}`
  - Returns JSON with `data` key
- **Block deals:** `https://www.nseindia.com/api/block-deal`
  - Returns today's block deals
- **No auth required (with proper headers)**
- **Fetch last 30 days on each run**

### 6.7 BSE Shareholding Patterns
- **URL:** `https://api.bseindia.com/BseIndiaAPI/api/ShareholdingPatterns/w?scripcode={BSE_CODE}&qtrid={QUARTER_ID}`
  - Quarter IDs: Q1 = March, Q2 = June, Q3 = September, Q4 = December
  - Requires BSE session cookie (same as insider trading)
- **Fetch:** Latest 2 quarters for comparison

---

## 7. Layer 1: Promoter Insider Trading (30%)

**File:** `india_alpha/signals/promoter_scorer.py`  
**Lookback:** Rolling 12 months  
**Cost:** Free — pure Python

### Signal Types and Point Values

| Signal Type | Base Points | Per-Type Cap |
|-------------|-------------|--------------|
| `open_market_buy` | +9 | +25 |
| `warrant_allotment` | +7 | +20 |
| `creeping_acquisition` | +7 | +20 |
| `pledge_decrease` | +6 | +15 |
| `preferential_allotment` | +3 | no cap |
| `esop_exercise` | +2 | no cap |
| `off_market` | +1 | no cap |
| `open_market_sell` | −5 | −20 |
| `pledge_increase` | −8 | −20 |

### Amplifiers

**Open market buy amplifiers** (stack multiplicatively):

| Condition | Multiplier |
|-----------|------------|
| Transaction value ≥ ₹2 Cr | ×1.3 |
| Transaction value ≥ ₹5 Cr | additional ×1.2 |
| 5+ buy transactions in 12m window | ×1.2 |

Example: ₹6 Cr buy, 5+ prior buys = `9 × 1.3 × 1.2 × 1.2 = 16.85` → capped at 25

**Pledge decrease amplifier:**

| Condition | Multiplier |
|-----------|------------|
| post-transaction pledge < 2% | ×1.5 |

### Score Computation

```python
for each signal in 12-month window:
    contribution = base_points * applicable_amplifiers
    type_total[signal_type] = min(type_total + contribution, per_type_cap)

raw_score = sum(type_total.values())
promoter_signal_score = clamp(raw_score, 0, 100)
```

### Output Fields Written to `india_promoter_summary`

- `promoter_signal_score` (int 0–100)
- `open_market_buying_cr_12m` (float)
- `open_market_selling_cr_12m` (float)
- `net_buying_cr_12m` (float)
- `buy_transaction_count_12m` (int)
- `pledge_trend` — "falling" | "rising" | "volatile" | "stable"
- `warrant_issued_12m` (bool)
- `creeping_acq_active` (bool)
- `highest_conviction_signal` (text — single most informative signal description)
- `score_narrative` (text — human-readable summary)

---

## 8. Layer 2: Operating Leverage Inflection (30%)

**File:** `india_alpha/signals/operating_leverage.py`  
**Input:** `india_financials_history` — annual periods, newest first, up to 5 years  
**Cost:** Free — pure Python

### Six Signals

**Signal 1: Debt → Net Cash Transition**

| Condition | Points | Label |
|-----------|--------|-------|
| net_debt_prev > 5 Cr AND net_debt_current < 0 | +25 | `debt_to_net_cash` |
| net_debt reduced > 60% (current < prev × 0.4) | +14 | `rapid_debt_reduction` |

`net_debt = total_debt_cr - cash_cr`

**Signal 2: EBITDA Margin Structural Expansion**

| Condition | Points | Label |
|-----------|--------|-------|
| 3yr expansion > 6pp AND 1yr > 1pp | +20 | `margin_structural_expansion` |
| 1yr expansion > 3pp | +12 | `margin_acceleration` |
| 1yr expansion > 1.5pp | +6 | (unlabelled) |

**Signal 3: ROCE Inflection**

| Condition | Points | Label |
|-----------|--------|-------|
| ROCE > 18% AND ROCE > (3yr_ago_ROCE + 6pp) | +15 | `roce_inflection` |
| ROCE > (prev_year + 3pp) AND ROCE > 12% | +8 | (unlabelled) |

**Signal 4: Revenue CAGR** (3-year: `(rev_now / rev_3yr)^(1/3) - 1`)

| Condition | Points | Label |
|-----------|--------|-------|
| CAGR > 22% | +15 | `revenue_hypergrowth` |
| CAGR 14–22% | +8 | (unlabelled) |
| CAGR 8–14% | +4 | (unlabelled) |

**Signal 5: Receivables Compression** (debtor days YoY)

| Condition | Points | Label |
|-----------|--------|-------|
| Decrease > 15 days | +12 | `receivables_compression` |
| Decrease > 8 days | +6 | (unlabelled) |

**Signal 6: Equity Value Creation**

| Condition | Points | Label |
|-----------|--------|-------|
| net_worth_current > net_worth_3yr × 1.8 | +10 | `equity_value_creation` |

### Score and Flags

```python
ol_score = clamp(sum_of_all_signals, 0, 100)
is_inflection_candidate = (ol_score >= 35 AND named_signals_count >= 2)
```

Maximum theoretical: 25 + 20 + 15 + 15 + 12 + 10 = 97

---

## 9. Layer 3: Corporate Intelligence (25%)

**File:** `india_alpha/signals/corporate_intelligence.py`  
**Input:** `india_corporate_filings` (NSE announcements, 12-month lookback)  
**Cost:** ~$0.01–$0.02 per Claude call; max 3 per company, max 40 per pipeline run

### Mode A: Python Rules Scoring (Zero Cost)

Every filing is scored by matching its `category` field:

| Category | Scoring Rule | Range |
|----------|-------------|-------|
| Credit Rating | "upgrade" → +15; "reaffirm" → +5; "watch positive" → +8; "downgrade" → −10; "watch negative" → −5 | −10 to +15 |
| Dividend | interim/special → +10; final → +5 | +5 to +10 |
| Appointment | CEO/CFO/MD → +5; Director → +3 | +3 to +5 |
| Resignation/Cessation | CFO/CEO/MD → −8; Director → −4 | −8 to −4 |
| Auditor Change | mid-term → −12; routine rotation → −2 | −12 to −2 |
| Acquisition/Merger | strategic/100% → +12; subsidiary/JV → +8; stake → +8 | +6 to +12 |
| Press Release | order win → +10; partnership → +8; expansion → +8; litigation → −3; adverse event → −5 | −5 to +10 |
| Takeover/Reg29 | stake increase → +8; disposal → −5 | −5 to +8 |
| Buyback | +10 | always +10 |
| Bonus/Split | bonus → +5; split → +3 | +3 to +5 |
| ESOP | +2 | always +2 |
| Other | +1 | +1 |

### Mode B: Claude Analysis (Cost-Controlled)

Trigger conditions (ALL must be true):
- `signal_priority == "HIGH"`
- `is_text_extracted == True`
- `word_count >= 200`
- `claude_calls_for_this_company < 3`
- `total_pipeline_claude_calls < 40`

Two prompt templates based on `category_bucket`:

**EARNINGS_ANALYSIS_PROMPT** (for quarterly results, guidance filings):

```
Score on 4 dimensions + 1 deduction. Return JSON only.
{
  "tone_score": 0-25,       // management tone: bullish=25, confident=20, neutral=12, cautious=6, defensive=0
  "forward_score": 0-25,    // strength and specificity of forward-looking statements
  "quant_score": 0-25,      // quantitative commitments (revenue/margin/capex targets with numbers)
  "hidden_insight_score": 0-10,  // what most analysts would miss
  "red_flag_deduction": 0 to -15, // concerns identified
  "reasoning": "brief explanation"
}
```

**CAPITAL_ACTION_PROMPT** (for acquisitions, buybacks, expansions):

```
Score on 4 dimensions + 1 deduction. Return JSON only.
{
  "strategic_score": 0-25,    // strategic value of the capital action
  "financial_score": 0-25,    // quantified financial impact
  "execution_score": 0-25,    // execution risk (low risk = high score)
  "hidden_insight_score": 0-10,
  "red_flag_deduction": 0 to -15,
  "reasoning": "brief explanation"
}
```

### Recency Multipliers (Applied to All Scores)

| Filing Age | Multiplier |
|------------|------------|
| ≤ 90 days | 1.0× |
| ≤ 180 days | 0.8× |
| ≤ 365 days | 0.5× |
| > 365 days | 0.3× |

### Bucket Aggregation

Group filing scores into 3 buckets:

| Bucket | Weight | Content |
|--------|--------|---------|
| EARNINGS_STRATEGY | 55% | Results, concall transcripts, guidance |
| CAPITAL_ACTION | 30% | Acquisitions, buybacks, expansions |
| GOVERNANCE | 15% | Management changes, auditor changes |

```python
earnings_bucket = sum(score * recency_mult for filings in EARNINGS_STRATEGY bucket), clamped 0-100
capital_bucket  = sum(score * recency_mult for filings in CAPITAL_ACTION bucket),  clamped 0-100
gov_bucket      = sum(score * recency_mult for filings in GOVERNANCE bucket),      clamped 0-100

# Claude scores normalized before aggregation
claude_normalized = (claude_score / 100) * 25 * recency_mult

corporate_intelligence_score = int(
    earnings_bucket * 0.55 +
    capital_bucket  * 0.30 +
    gov_bucket      * 0.15
)
```

---

## 10. Layer 4: Policy Tailwind (10%)

**File:** `india_alpha/signals/policy_scorer.py`  
**Data file:** `india_alpha/data/policy_registry.json`  
**Cost:** Free — pure Python

### 15 Active Policies

| Policy ID | Name | Impact | Points |
|-----------|------|--------|--------|
| `pli_electronics` | PLI IT Hardware & Electronics | HIGH | 15 |
| `pli_pharma` | PLI Pharmaceuticals & API | HIGH | 15 |
| `pli_auto` | PLI Auto & Auto Components | HIGH | 15 |
| `pli_solar` | PLI Solar PV Manufacturing | HIGH | 15 |
| `defense_indigenization` | Defense Indigenization & Positive List | HIGH | 15 |
| `semiconductor_fab` | India Semiconductor Mission | HIGH | 15 |
| `pm_gati_shakti` | PM Gati Shakti Infrastructure | HIGH | 15 |
| `fame_ev` | FAME III / EV Ecosystem | HIGH | 15 |
| `pli_textiles` | PLI Textiles (MMF & Technical) | MEDIUM | 10 |
| `pli_food` | PLI Food Processing | MEDIUM | 10 |
| `pli_steel` | PLI Specialty Steel | MEDIUM | 10 |
| `green_hydrogen` | National Green Hydrogen Mission | MEDIUM | 10 |
| `digital_india` | Digital India & IT Modernization | MEDIUM | 10 |
| `pm_awas_yojana` | PM Awas Yojana Housing | MEDIUM | 10 |
| `china_plus_one` | China+1 Manufacturing Shift | MEDIUM | 10 |

### Matching Logic

A company matches a policy if **either** is true:
1. `company.sector` (case-insensitive) contains or is contained in any of `policy.beneficiary_sectors`
2. Any of `policy.beneficiary_keywords` appears in `company.sector + " " + company.industry` (lowercase)

```python
policy_score = clamp(sum(policy.points for each matching policy), 0, 100)
```

Matching is determined from `india_alpha/data/policy_registry.json`. Do not hardcode keywords in the Python file — always read from the JSON.

---

## 11. Layer 5: Quality Emergence (5%)

**File:** `india_alpha/signals/quality_scorer.py`  
**Input:** `india_financials_history` — annual, newest first, minimum 3 years  
**Cost:** Free — pure Python

### Six Signals

**Signal 1: ROE Breakout (+20)**

```
condition: current_roe >= 15% AND average(prior_years_roe) < 13%
label: roe_breakout
```

**Signal 2: ROCE Consistency (+18)**

```
condition: current_roce > 15% AND previous_year_roce > 15% AND current_roce > previous_year_roce
label: roce_consistency
```

**Signal 3: Margin Expansion Streak (+18 or +10)**

```
streak_3: margin[year0] > margin[year1] > margin[year2] AND (margin[year0] - margin[year2]) > 3pp → +18, label: margin_expansion_streak
streak_2: margin[year0] > margin[year1] AND (margin[year0] - margin[year1]) > 2pp → +10 (no label)
```

**Signal 4: Deleveraging (+16)**

```
de_now = total_debt / net_worth (current year)
peak_de = max(total_debt / net_worth across all history years)
condition: de_now < 0.5 AND peak_de > 0.8
label: deleveraging
```

**Signal 5: Working Capital Tightening (+14 or +7)**

```
wc_cycle = debtor_days + inventory_days
consecutive_3yr_decline → +14, label: working_capital_tightening
consecutive_2yr_decline → +7 (no label)
```

**Signal 6: Earnings Quality (+14)**

```
condition: pat_margin_current > pat_margin_oldest + 1pp AND revenue_cagr > 10%
label: earnings_quality
```

### Convergence Multiplier

```python
if named_signals_count >= 4:
    raw_score = raw_score * 1.15

quality_score = clamp(raw_score, 0, 100)
```

Maximum theoretical: (20 + 18 + 18 + 16 + 14 + 14) × 1.15 = 115 → capped at 100

---

## 12. Modifier 1: Valuation Gate

**File:** `india_alpha/signals/valuation_scorer.py`  
**Input:** `india_companies` (yfinance data already stored)  
**Effect:** Multiplicative on base_composite (0.75× to 1.15×)

### Five Dimensions

**Dimension 1: PE vs Sector Median (25 pts max)**

Sector median PE = median of all companies in same sector with valid positive PE (require ≥ 3 companies; otherwise skip this dimension).

| Company PE / Sector Median | Points |
|----------------------------|--------|
| < 0.50× | 25 |
| 0.50× – 0.75× | 18 |
| 0.75× – 1.00× | 12 |
| 1.00× – 1.50× | 6 |
| > 1.50× | 0 |

**Dimension 2: Absolute PE (25 pts max)**

| Trailing PE | Points |
|-------------|--------|
| < 8× | 25 |
| 8× – 15× | 18 |
| 15× – 25× | 10 |
| 25× – 40× | 3 |
| > 40× or negative | 0 |

**Dimension 3: Price-to-Book (15 pts max)**

| P/B | Points |
|-----|--------|
| < 1.0× | 15 |
| 1.0× – 2.0× | 10 |
| 2.0× – 4.0× | 5 |
| 4.0× – 6.0× | 2 |
| > 6.0× | 0 |

**Dimension 4: EV/EBITDA (20 pts max)**

| EV/EBITDA | Points |
|-----------|--------|
| < 6× | 20 |
| 6× – 10× | 14 |
| 10× – 15× | 8 |
| 15× – 20× | 3 |
| > 20× | 0 |

**Dimension 5: 52-Week Position (15 pts + 3 pt bonus)**

`position = (current_price - low_52w) / (high_52w - low_52w) × 100`

| Position | Points |
|----------|--------|
| 0–20% (near low) | 15 |
| 20–40% | 10 |
| 40–60% | 6 |
| 60–80% | 3 |
| 80–100% | 0 |

Bonus: if `current_price < dma_200`, add +3 to this dimension.

### Missing Data Handling

- Zero dimensions with data → default `valuation_score = 35`, zone = FAIR, multiplier = 1.00
- 2 or fewer dimensions with data → impute missing dimensions at `0.60 × avg_scored_proportion × max_points_for_dimension`

### Zones and Multipliers

```python
valuation_score = clamp(sum_of_all_dimensions, 0, 100)
```

| Zone | Score Range | Multiplier |
|------|-------------|------------|
| DEEP_VALUE | ≥ 75 | 1.15× |
| CHEAP | 55 – 74 | 1.08× |
| FAIR | 35 – 54 | 1.00× |
| EXPENSIVE | 20 – 34 | 0.90× |
| OVERVALUED | < 20 | 0.75× |

---

## 13. Modifier 2: Smart Money Bonus

**File:** `india_alpha/signals/smart_money_scorer.py`  
**Input:** `india_shareholding_patterns` (latest 2 quarters) + `india_bulk_deals` (last 30 days)  
**Effect:** Additive (−10 to +15)

### 25 Tracked Superstar Investors

Track by name AND aliases in shareholding data:

1. Ashish Kacholia (Lucky Securities)
2. Vijay Kedia (Kedia Securities)
3. Dolly Khanna (Rajiv Khanna)
4. Mohnish Pabrai (Dalal Street LLC)
5. Radhakishan Damani (Bright Star Investments)
6. RARE Enterprises / Rekha Jhunjhunwala
7. Mukul Agrawal (Mukul Mahavir Prasad Agrawal)
8. Sunil Singhania (Abakkus Growth Fund)
9. Porinju Veliyath (Equity Intelligence)
10. Anil Kumar Goel
11. Madhusudan Kela (MK Ventures)
12. Nemish Shah (Shrevyas Investments)
13. Ramesh Damani
14. Shankar Sharma (First Global)
15. Akash Bhanshali (Enam Holdings)
16. Sanjay Bakshi
17. Basant Maheshwari (Basant Maheshwari Wealth Advisers)
18. Kenneth Andrade (Old Bridge Capital)
19. Sameer Narayan
20. Raamdeo Agrawal (Motilal Oswal)
21. Vallabh Bhanshali
22. S Naren (Sankaran Naren)
23. Sumeet Nagar (Malabar Investments)
24. Saurabh Mukherjea (Marcellus Investment)
25. Amit Jeswani (Stallion Asset)

### Institutional Classification for Bulk Deals

A bulk deal client is `is_institutional = True` if name contains any of: mutual fund, insurance, pension, bank, fii, fpi, securities, capital, asset management, amc, hedge fund, goldman sachs, morgan stanley, jp morgan, lic of india, sbi mutual, hdfc mutual, icici prudential, axis mutual, kotak mahindra, dsp, franklin templeton, nippon india

### Nine Signals

| # | Signal | Points | Condition |
|---|--------|--------|-----------|
| 1 | Superstar new entry | +10 | In current quarter, not in previous quarter |
| 2 | Superstar increased | +6 | Higher % in current vs previous quarter |
| 3 | Superstar exited | −8 | In previous quarter, not in current quarter |
| 4 | MF accumulation strong | +6 | MF holding increased > 1.0pp QoQ |
| 5 | MF accumulation moderate | +3 | MF holding increased 0.5–1.0pp QoQ |
| 6 | MF exit | −4 | MF holding decreased > 1.0pp QoQ |
| 7 | FII accumulation | +4 | FII holding increased > 0.5pp QoQ |
| 8 | Institutional bulk buy | +5 | Institutional BUY in last 30 days |
| 9 | Institutional bulk sell | −5 | Institutional SELL in last 30 days |

Signals 4 and 5 are mutually exclusive — only the higher one fires.

```python
raw_total = sum(points for all fired signals)
smart_money_score = clamp(raw_total, -10, +15)
```

---

## 14. Modifier 3: Degradation Penalty

**File:** `india_alpha/signals/degradation_monitor.py`  
**Input:** Cross-references all existing tables. No new external data fetches.  
**Effect:** Subtractive (−30 to 0)

### Ten Red Flags

| # | Flag | Penalty | Source | Lookback | Condition |
|---|------|---------|--------|----------|-----------|
| 1 | Net insider selling | −8 | `india_promoter_signals` | 90 days | sell_value_cr > buy_value_cr + 0.5 |
| 2 | Pledge increasing | −6 | `india_promoter_signals` | 90 days | most recent pledge_increase: post_pct > pre_pct |
| 3 | Multiple insiders selling | −5 | `india_promoter_signals` | 90 days | ≥ 3 distinct person names with open_market_sell |
| 4 | EBITDA margin declining | −6 | `india_financials_history` (quarterly) | 3 quarters | 2+ consecutive quarters of margin decline |
| 5 | Revenue declining YoY | −5 | `india_financials_history` (annual) | 2 years | latest annual revenue < previous annual revenue |
| 6 | Leverage up, margins down | −5 | `india_financials_history` | 2 years | D/E rose AND EBITDA margin fell simultaneously |
| 7 | Auditor change mid-term | −8 | `india_corporate_filings` | 12 months | filing category "auditor" with change/resign keywords |
| 8 | Key management resignation | −6 | `india_corporate_filings` | 12 months | "resign" AND (CFO/CEO/MD/Chief Executive/Chief Financial) |
| 9 | Credit rating downgrade | −5 | `india_corporate_filings` | 12 months | "credit rating" AND "downgrade" in subject |
| 10 | Price below 200-DMA | −3 | `india_companies` | current | current_price < dma_200 × 0.85 |

Flags 7, 8, 9 trigger at most once each per company.

```python
raw_penalty = sum(penalty for each triggered flag)
degradation_score = max(-30, raw_penalty)          # Floor at -30
is_degrading = (degradation_score <= -15)
```

---

## 15. Composite Scoring — The Final Formula

**File:** `india_alpha/processing/gem_scorer.py`

### Step 1: Rescale Each Layer Score (Piecewise Linear)

Raw scores cluster in the 10–30 range. Rescaling spreads them meaningfully across 0–100 for weighted averaging.

**Formula for piecewise interpolation:**
```python
def rescale(raw, table):
    # table = list of (raw_breakpoint, rescaled_breakpoint) tuples
    if raw <= table[0][0]: return table[0][1]
    if raw >= table[-1][0]: return table[-1][1]
    for i in range(len(table)-1):
        x0, y0 = table[i]
        x1, y1 = table[i+1]
        if x0 <= raw <= x1:
            t = (raw - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
```

**Rescaling tables (exact — do not modify):**

| Raw | Promoter → | OL → | Corp Intel → | Policy → | Quality → |
|-----|-----------|------|-------------|---------|----------|
| 0 | 0 | 0 | 0 | 0 | 0 |
| 8 | — | — | 20 | — | — |
| 10 | 25 | 20 | — | 25 | 25 |
| 18 | — | — | 50 | — | — |
| 20 | 50 | — | — | 50 | 50 |
| 25 | — | 50 | — | — | — |
| 30 | 70 | — | 70 | — | 70 |
| 35 | — | — | — | 70 | — |
| 40 | — | 70 | — | — | — |
| 45 | — | — | 85 | — | — |
| 50 | 85 | — | — | 85 | 85 |
| 55 | — | 85 | — | — | — |
| 70 | — | — | 95 | — | — |
| 75 | 95 | 95 | — | 95 | 95 |
| 100 | 100 | 100 | 100 | 100 | 100 |

**Verification:** raw promoter = 15 → interpolate between (10,25) and (20,50) → t = 0.5 → result = 37.5

### Step 2: Dynamic Weighted Average

Layer weights:

| Layer | Weight |
|-------|--------|
| Promoter | 30% |
| Operating Leverage | 30% |
| Corporate Intelligence | 25% |
| Policy Tailwind | 10% |
| Quality Emergence | 5% |

Only layers with `raw_score > 0` participate. Weights renormalized to sum to 1.0:

```python
active = {layer: weight for layer, weight in WEIGHTS if raw_score[layer] > 0}
total_w = sum(active.values())
base = sum(rescaled[layer] * w for layer, w in active.items()) / total_w
```

Example with only Promoter (30%) and OL (30%) active:
- total_w = 0.60
- base = (promoter_rescaled × 0.30 + ol_rescaled × 0.30) / 0.60

### Step 3: Convergence Bonus

```python
layers_ge_40 = count(rescaled_score >= 40 for each layer)

if layers_ge_40 >= 4:   convergence_mult = 1.15
elif layers_ge_40 >= 3: convergence_mult = 1.10
elif layers_ge_40 >= 2: convergence_mult = 1.06
else:                   convergence_mult = 1.00

base_composite = min(100, base * convergence_mult)
```

### Step 4: Apply Three Modifiers

```python
final_score = clamp(
    base_composite * valuation_multiplier + smart_money_bonus + degradation_penalty,
    0, 100
)
```

Note: valuation_multiplier defaults to 1.00 if no valuation data. smart_money_bonus defaults to 0. degradation_penalty defaults to 0.

### Step 5: Conviction Tier Assignment

```python
if final_score >= 70:  tier = "HIGHEST"
elif final_score >= 58: tier = "HIGH"
elif final_score >= 45: tier = "MEDIUM"
elif final_score >= 30: tier = "WATCH"
else:                   tier = "BELOW_THRESHOLD"
```

### Worked Example Verification

Use this to verify the composite scorer is implemented correctly:

| Input | Value |
|-------|-------|
| Promoter raw | 22 → rescaled 54.0 |
| OL raw | 41 → rescaled 71.0 |
| Corp Intel raw | 18 → rescaled 50.0 |
| Policy raw | 25 → rescaled 56.7 |
| Quality raw | 34 → rescaled 73.0 |

All 5 layers active, total_w = 1.00:
```
base = 54.0×0.30 + 71.0×0.30 + 50.0×0.25 + 56.7×0.10 + 73.0×0.05
     = 16.2 + 21.3 + 12.5 + 5.67 + 3.65 = 59.32
```

All 5 rescaled scores ≥ 40 → convergence_mult = 1.15x:
```
base_composite = min(100, 59.32 × 1.15) = 68.2
```

Modifiers: valuation_mult = 1.08 (CHEAP zone), smart_money = +13, degradation = 0:
```
final = clamp(68.2 × 1.08 + 13 + 0, 0, 100)
      = clamp(73.66 + 13, 0, 100)
      = 86.7
```

Tier: 86.7 ≥ 70 → **HIGHEST**

**If your implementation does not produce 86.7 for these inputs, your composite scorer is wrong.**

### Additional Flags Written to `india_hidden_gems`

```python
is_below_institutional = (market_cap_cr < 2500)
is_pre_discovery = (analyst_count < 3)
layers_firing = count(rescaled >= 40)
```

---

## 16. Claude Thesis Synthesis

**Triggered only for:** HIGH and HIGHEST conviction tiers  
**Cost:** ~$0.005 per synthesis (≤ 800 tokens)  
**Model:** `claude-sonnet-4-6`

### Prompt Design Principles

1. Pass the rescaled scores (not raw) — they communicate relative signal strength better
2. Pass narratives from each layer scorer as context
3. Pass active signals list (top 2–3 per layer)
4. Specify the exact JSON fields required
5. Instruct Claude to be honest — a weak thesis is preferable to a fabricated strong one

### Required Output Fields

The prompt must extract exactly these fields (all text, all required):

```json
{
  "gem_thesis": "3 sentences: what company does, why undiscovered, what changes narrative",
  "key_catalyst": "specific re-rating trigger — concrete event, not vague",
  "catalyst_timeline": "quarter format: Q2FY27 or H2FY26",
  "catalyst_confidence": "high|medium|low",
  "primary_risk": "single biggest thesis-breaking risk",
  "what_market_misses": "specific classification error the market is making",
  "entry_note": "price or condition for optimal entry"
}
```

### JSON Parsing Safety

Always handle the case where Claude wraps JSON in markdown code fences:

```python
try:
    return json.loads(text)
except json.JSONDecodeError:
    import re
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        return json.loads(m.group())
```

---

## 17. Build Status: What Exists vs What's Missing

### ✅ Built and Tested

| File | Status |
|------|--------|
| `config.py` | Complete |
| `db.py` | Complete |
| `fetchers/universe_builder.py` | Complete — NSE CSV + yfinance |
| `fetchers/bse_insider.py` | Complete — BSE API + insiderscreener fallback |
| `fetchers/screener_fetcher.py` | Complete — session cookie auth, 1.2s rate limit |
| `signals/promoter_scorer.py` | Complete — all weights, amplifiers, caps |
| `signals/operating_leverage.py` | Complete — all 6 signals |
| `signals/policy_scorer.py` | Complete — 15 policies, JSON-driven |
| `signals/quality_scorer.py` | Complete — all 6 signals |
| `signals/valuation_scorer.py` | Complete — all 5 dimensions, zone/multiplier |
| `signals/degradation_monitor.py` | Complete — all 10 red flags |
| `processing/gem_scorer.py` | Complete — full formula, rescaling, convergence, all modifiers |
| `data/policy_registry.json` | Complete — all 15 policies with keywords |
| `schema.sql` | Complete — all 17 tables + indexes |
| `scripts/run_pipeline.py` | Complete — step-by-step runner |
| `scripts/test_connection.py` | Complete — DB + logic smoke tests |

### ❌ Not Yet Built

| File | What It Needs to Do |
|------|-------------------|
| `fetchers/nse_filings_fetcher.py` | Fetch NSE corporate announcements for all companies. URL: `https://www.nseindia.com/api/corp-info?symbol={SYM}&corpType=announcements`. Rate limit 1.5s. Store to `india_corporate_filings`. Classify each filing into category_bucket (EARNINGS_STRATEGY / CAPITAL_ACTION / GOVERNANCE). Mark signal_priority HIGH if category is: credit rating upgrade, acquisition, press release with order win, key management change. |
| `signals/corporate_intelligence.py` | Read `india_corporate_filings`, apply Python rules (Mode A) to all filings, apply Claude analysis (Mode B) to HIGH priority filings with extracted text, aggregate into 3 buckets, write to `india_corporate_intelligence_scores`. Max 3 Claude calls per company, 40 per pipeline run. |
| `fetchers/nse_bulk_deals_fetcher.py` | Fetch last 30 days of bulk/block deals from NSE. URL: `https://www.nseindia.com/api/historical/bulk-deals?from=YYYY-MM-DD&to=YYYY-MM-DD`. Classify each deal as institutional/superstar. Write to `india_bulk_deals`. |
| `fetchers/shareholding_fetcher.py` | Fetch latest 2 quarters of shareholding patterns from BSE. Identify superstar holdings from `india_alpha/data/superstar_investors.json` (create this file — list of 25 investors with name variants). Write to `india_shareholding_patterns`. |
| `signals/smart_money_scorer.py` | Read last 2 quarters from `india_shareholding_patterns` and last 30 days from `india_bulk_deals`. Apply 9 signals. Write to `india_smart_money_scores`. Score = clamp(raw, −10, +15). |
| `api/routes.py` | FastAPI routes: GET /gems (returns top hidden gems), GET /gem/{ticker} (single company detail), GET /pipeline/status (job run history), POST /pipeline/run (trigger pipeline). |
| `main.py` | FastAPI app instantiation on port 8001. |

### ⚠️ Data Files Needed

| File | Contents |
|------|---------|
| `india_alpha/data/superstar_investors.json` | 25 investor names with all known aliases (for shareholding pattern matching) |

---

## 18. Next Build Session — Exact Tasks

Build in this order. Do not skip ahead.

### Task 1: NSE Filings Fetcher (`fetchers/nse_filings_fetcher.py`)

```
Input:  List of all tickers from india_companies
Output: Write to india_corporate_filings

For each ticker:
  1. GET https://www.nseindia.com/api/corp-info?symbol={ticker}&corpType=announcements
     Headers: User-Agent, Accept: application/json, Referer: https://www.nseindia.com/
  2. Parse response['data'] list
  3. For each announcement:
     - Map 'category' to one of the known categories (credit rating/dividend/appointment etc)
     - Assign category_bucket (EARNINGS_STRATEGY | CAPITAL_ACTION | GOVERNANCE)
     - Assign signal_priority (HIGH if credit upgrade / acquisition / order win press release / MD resignation)
     - Compute recency_multiplier based on days_old
  4. Upsert to india_corporate_filings on conflict (isin, filing_date, subject)
  5. Sleep 1.5s between tickers
```

### Task 2: Corporate Intelligence Scorer (`signals/corporate_intelligence.py`)

```
For each company with filings:
  1. Load all filings from india_corporate_filings (last 12 months)
  2. Apply Python rules (Mode A) to every filing → python_score × recency_multiplier
  3. For HIGH priority filings with text (Mode B):
     - Check company_claude_count < 3 AND total_pipeline_count < 40
     - If conditions met: call Claude with appropriate prompt template
     - Store claude_score on the filing record
  4. Aggregate into 3 buckets with weights 0.55 / 0.30 / 0.15
  5. Write corporate_intelligence_score to india_corporate_intelligence_scores
```

### Task 3: Bulk Deals Fetcher (`fetchers/nse_bulk_deals_fetcher.py`)

```
Fetch last 30 days of bulk and block deals from NSE API
Classify each deal:
  - is_institutional: check client_name against 40+ institutional keywords
  - is_superstar: check client_name against superstar investor names + aliases
Write to india_bulk_deals
```

### Task 4: Shareholding Fetcher (`fetchers/shareholding_fetcher.py`)

```
For each company (requires bse_code in india_companies):
  Fetch latest 2 quarters from BSE shareholding API
  Parse: promoter_pct, fii_pct, dii_pct, mf_pct, public_pct
  Parse individual large holders from the detailed breakdown
  Check each against superstar_investors.json
  Write to india_shareholding_patterns
```

### Task 5: Smart Money Scorer (`signals/smart_money_scorer.py`)

```
For each company:
  Load latest 2 quarters from india_shareholding_patterns
  Load last 30 days from india_bulk_deals
  Apply 9 signals (see Section 13)
  smart_money_score = clamp(raw, -10, +15)
  Write to india_smart_money_scores
```

### Task 6: API Routes (`api/routes.py` + `main.py`)

```
GET /gems?tier=HIGH&limit=20 → top hidden gems sorted by final_score DESC
GET /gem/{ticker} → full detail including all layer scores and thesis
GET /pipeline/status → last 10 job runs from india_job_runs
POST /pipeline/run?step=all → trigger pipeline asynchronously
```

---

## Appendix: Market Cap Tier Classification

```python
def get_market_cap_tier(mcap_cr: float) -> str:
    if mcap_cr >= 20000: return "LARGE"
    if mcap_cr >= 5000:  return "MID"
    if mcap_cr >= 500:   return "SMALL"
    if mcap_cr >= 50:    return "MICRO"
    return "NANO"
```

Universe target: Companies in MID, SMALL, MICRO tiers (₹50 Cr – ₹20,000 Cr).

## Appendix: Quarter Format Conventions

- Current quarter ID format: Q1FY27, Q2FY27, Q3FY27, Q4FY27
- Q1 = April–June, Q2 = July–September, Q3 = October–December, Q4 = January–March
- Financial year is April to March (India standard)
- When computing QoQ shareholding deltas: always compare most recent quarter to the immediately prior quarter

## Appendix: NSE Request Headers (Required)

NSE blocks requests without proper browser headers:

```python
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}
```

Always establish an NSE session first by hitting the homepage before API calls:
```python
async with httpx.AsyncClient(headers=NSE_HEADERS) as client:
    await client.get("https://www.nseindia.com/")  # Establish session + cookies
    # Then make API calls using same client instance
```

---

*End of Master Build Specification.*  
*Version 1.0 — March 2026 — JIP Horizon India*
