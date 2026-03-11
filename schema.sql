-- ════════════════════════════════════════════════════════════════
-- JIP HORIZON INDIA — Complete Supabase Schema
-- Run this ONCE in your Supabase SQL editor
-- All tables prefixed with india_ to coexist with global JIP tables
-- ════════════════════════════════════════════════════════════════

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ─────────────────────────────────────────────────────────────
-- 1. MASTER COMPANY REGISTRY
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_companies (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ticker          TEXT NOT NULL,
    exchange        TEXT NOT NULL DEFAULT 'NSE',
    nse_symbol      TEXT,
    bse_code        TEXT,
    company_name    TEXT NOT NULL,
    isin            TEXT,

    -- Classification
    sector          TEXT,
    industry        TEXT,
    market_cap_cr   FLOAT,
    market_cap_tier TEXT,    -- LARGE|MID|SMALL|MICRO|NANO

    -- Fundamentals (refreshed weekly)
    revenue_cr_ttm      FLOAT,
    revenue_growth_yoy  FLOAT,
    ebitda_margin       FLOAT,
    pat_cr_ttm          FLOAT,
    roe                 FLOAT,
    debt_equity         FLOAT,

    -- Coverage
    analyst_count       INTEGER DEFAULT 0,
    is_f_and_o          BOOLEAN DEFAULT false,
    is_index_stock      BOOLEAN DEFAULT false,

    -- Metadata
    first_seen          DATE,
    last_updated        TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(exchange, ticker)
);

-- ─────────────────────────────────────────────────────────────
-- 2. LAYER 1 — PROMOTER SIGNAL TABLES
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_promoter_signals (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    company_name    TEXT,

    signal_type     TEXT NOT NULL,
    -- open_market_buy | open_market_sell | warrant_allotment
    -- pledge_increase | pledge_decrease | creeping_acquisition
    -- preferential_allotment | esop_exercise | off_market | other

    transaction_date    DATE NOT NULL,
    intimation_date     DATE,

    -- Who
    person_name         TEXT,
    person_category     TEXT,    -- promoter|promoter_group|director|kmp

    -- What
    transaction_type    TEXT,
    shares              BIGINT,
    price_per_share     FLOAT,
    value_cr            FLOAT,
    post_transaction_pct FLOAT,

    -- Pledge specific
    pledged_shares_before   BIGINT,
    pledged_shares_after    BIGINT,
    pledge_pct_before       FLOAT,
    pledge_pct_after        FLOAT,

    -- Signal strength (pre-computed)
    signal_strength     INTEGER DEFAULT 0,

    source_url          TEXT,
    raw_data            JSONB,
    fetched_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS india_promoter_summary (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin            TEXT UNIQUE NOT NULL,
    ticker          TEXT NOT NULL,

    -- Rolling 12-month aggregates
    open_market_buying_cr_12m   FLOAT DEFAULT 0,
    open_market_selling_cr_12m  FLOAT DEFAULT 0,
    net_buying_cr_12m           FLOAT DEFAULT 0,
    buy_transaction_count_12m   INTEGER DEFAULT 0,
    pledge_trend                TEXT,
    warrant_issued_12m          BOOLEAN DEFAULT false,
    creeping_acq_active         BOOLEAN DEFAULT false,

    -- Computed score
    promoter_signal_score       INTEGER DEFAULT 0,
    score_narrative             TEXT,
    highest_conviction_signal   TEXT,

    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- 3. LAYER 2 — FINANCIAL HISTORY + OPERATING LEVERAGE
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_financials_history (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin        TEXT NOT NULL,
    ticker      TEXT NOT NULL,

    period_type TEXT NOT NULL,   -- annual|quarterly
    period_end  DATE NOT NULL,

    -- P&L
    revenue_cr          FLOAT,
    revenue_growth_yoy  FLOAT,
    ebitda_cr           FLOAT,
    ebitda_margin_pct   FLOAT,
    pat_cr              FLOAT,
    pat_margin_pct      FLOAT,
    eps                 FLOAT,

    -- Balance Sheet
    total_debt_cr       FLOAT,
    cash_cr             FLOAT,
    net_worth_cr        FLOAT,
    total_assets_cr     FLOAT,

    -- Working Capital
    debtor_days         FLOAT,
    inventory_days      FLOAT,
    creditor_days       FLOAT,

    -- Efficiency
    roce                FLOAT,
    roe                 FLOAT,

    -- Optional (from concalls / annual reports)
    export_revenue_cr       FLOAT,
    capacity_utilisation_pct FLOAT,
    order_book_cr           FLOAT,

    source      TEXT DEFAULT 'yfinance',
    fetched_at  TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(isin, period_type, period_end)
);

CREATE TABLE IF NOT EXISTS india_operating_leverage_scores (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin        TEXT UNIQUE NOT NULL,
    ticker      TEXT NOT NULL,

    -- Score
    ol_score                INTEGER DEFAULT 0,
    signals_firing          INTEGER DEFAULT 0,
    is_inflection_candidate BOOLEAN DEFAULT false,

    -- Details
    active_signals          JSONB DEFAULT '[]',
    score_narrative         TEXT,

    scored_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- 4a. LAYER 5 — QUALITY EMERGENCE
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_quality_scores (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin            TEXT UNIQUE NOT NULL,
    ticker          TEXT NOT NULL,

    -- Score
    quality_score           INTEGER DEFAULT 0,
    signals_firing          INTEGER DEFAULT 0,

    -- Details
    active_signals          JSONB DEFAULT '[]',
    score_narrative         TEXT,

    scored_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- 4b. LAYER 4 — POLICY TAILWIND
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_policy_scores (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin            TEXT UNIQUE NOT NULL,
    ticker          TEXT NOT NULL,

    -- Score
    policy_score            INTEGER DEFAULT 0,
    matching_policies       JSONB DEFAULT '[]',
    score_narrative         TEXT,

    scored_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- 4c. LAYER 3 — CORPORATE INTELLIGENCE (NSE Filings)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_corporate_filings (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin              TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    company_name      TEXT,
    nse_seq_id        TEXT NOT NULL,
    announcement_date TIMESTAMPTZ NOT NULL,
    sort_date         DATE NOT NULL,
    category          TEXT NOT NULL,
    category_bucket   TEXT NOT NULL,        -- earnings_strategy | capital_action | governance
    signal_priority   TEXT DEFAULT 'LOW',   -- HIGH | MEDIUM | LOW
    subject_text      TEXT,
    pdf_url           TEXT,
    pdf_size_bytes    INTEGER,
    extracted_text    TEXT,
    word_count        INTEGER DEFAULT 0,
    extraction_method TEXT,                 -- pdfplumber | subject_only | failed
    is_downloaded     BOOLEAN DEFAULT false,
    is_text_extracted BOOLEAN DEFAULT false,
    is_analysed       BOOLEAN DEFAULT false,
    analysed_at       TIMESTAMPTZ,
    raw_json          JSONB,
    fetched_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(isin, nse_seq_id)
);

CREATE TABLE IF NOT EXISTS india_corporate_intelligence_scores (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                        TEXT UNIQUE NOT NULL,
    ticker                      TEXT NOT NULL,
    corporate_intelligence_score INTEGER DEFAULT 0,
    earnings_strategy_score     INTEGER DEFAULT 0,
    capital_action_score        INTEGER DEFAULT 0,
    governance_score            INTEGER DEFAULT 0,
    management_tone             TEXT,
    key_forward_signals         JSONB DEFAULT '[]',
    key_capital_actions         JSONB DEFAULT '[]',
    governance_flags            JSONB DEFAULT '[]',
    hidden_insight              TEXT,
    filings_analysed            INTEGER DEFAULT 0,
    filings_available           INTEGER DEFAULT 0,
    latest_filing_date          DATE,
    score_narrative             TEXT,
    scored_at                   TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- 4c-legacy. CONCALL INTELLIGENCE (deprecated — kept for backward compat)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_concalls (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    company_name    TEXT,
    quarter         TEXT NOT NULL,    -- Q1FY27, Q2FY27 etc
    call_date       DATE,
    transcript_url  TEXT,
    transcript_text TEXT,
    word_count      INTEGER,
    is_processed    BOOLEAN DEFAULT false,
    processed_at    TIMESTAMPTZ,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(isin, quarter)
);

CREATE TABLE IF NOT EXISTS india_concall_signals (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    concall_id      UUID REFERENCES india_concalls(id) ON DELETE CASCADE,
    isin            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    quarter         TEXT NOT NULL,

    management_tone         TEXT,
    tone_reasoning          TEXT,
    investability_signal    TEXT,
    forward_signals         JSONB DEFAULT '[]',
    quantitative_commitments JSONB DEFAULT '[]',
    red_flags               JSONB DEFAULT '[]',
    order_book_cr           FLOAT,
    hidden_insight          TEXT,
    concall_signal_score    INTEGER DEFAULT 0,
    score_reasoning         TEXT,

    processed_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- 5. COMPOSITE OUTPUT TABLE
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_hidden_gems (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin            TEXT UNIQUE NOT NULL,
    ticker          TEXT NOT NULL,
    company_name    TEXT NOT NULL,
    exchange        TEXT DEFAULT 'NSE',
    market_cap_cr   FLOAT,
    analyst_count   INTEGER DEFAULT 0,

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
    base_composite              FLOAT,
    final_score                 FLOAT,
    conviction_tier             TEXT,
    -- Discovery flags
    is_pre_discovery            BOOLEAN DEFAULT false,
    is_below_institutional      BOOLEAN DEFAULT false,
    layers_firing               INTEGER DEFAULT 0,

    -- Thesis (Claude-generated for HIGH/HIGHEST only)
    gem_thesis          TEXT,
    key_catalyst        TEXT,
    catalyst_timeline   TEXT,
    catalyst_confidence TEXT,
    primary_risk        TEXT,
    what_market_misses  TEXT,
    entry_note          TEXT,

    scored_at       TIMESTAMPTZ DEFAULT NOW(),
    last_updated    TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- 5a. VALUATION COLUMNS ON india_companies
-- ─────────────────────────────────────────────────────────────
ALTER TABLE india_companies ADD COLUMN IF NOT EXISTS trailing_pe FLOAT;
ALTER TABLE india_companies ADD COLUMN IF NOT EXISTS forward_pe FLOAT;
ALTER TABLE india_companies ADD COLUMN IF NOT EXISTS price_to_book FLOAT;
ALTER TABLE india_companies ADD COLUMN IF NOT EXISTS ev_to_ebitda FLOAT;
ALTER TABLE india_companies ADD COLUMN IF NOT EXISTS dividend_yield FLOAT;
ALTER TABLE india_companies ADD COLUMN IF NOT EXISTS current_price FLOAT;
ALTER TABLE india_companies ADD COLUMN IF NOT EXISTS fifty_two_week_low FLOAT;
ALTER TABLE india_companies ADD COLUMN IF NOT EXISTS fifty_two_week_high FLOAT;
ALTER TABLE india_companies ADD COLUMN IF NOT EXISTS two_hundred_dma FLOAT;
ALTER TABLE india_companies ADD COLUMN IF NOT EXISTS free_cash_flow FLOAT;

-- ─────────────────────────────────────────────────────────────
-- 5b. MODIFIER & AUDIT COLUMNS ON india_hidden_gems
-- (migration for existing deployments — new deployments get these in CREATE TABLE above)
-- ─────────────────────────────────────────────────────────────
ALTER TABLE india_hidden_gems ADD COLUMN IF NOT EXISTS valuation_multiplier FLOAT DEFAULT 1.00;
ALTER TABLE india_hidden_gems ADD COLUMN IF NOT EXISTS smart_money_bonus INTEGER DEFAULT 0;
ALTER TABLE india_hidden_gems ADD COLUMN IF NOT EXISTS degradation_penalty INTEGER DEFAULT 0;
ALTER TABLE india_hidden_gems ADD COLUMN IF NOT EXISTS base_composite FLOAT;
ALTER TABLE india_hidden_gems ADD COLUMN IF NOT EXISTS final_score FLOAT;
ALTER TABLE india_hidden_gems ADD COLUMN IF NOT EXISTS is_degrading BOOLEAN DEFAULT false;

-- ─────────────────────────────────────────────────────────────
-- 5c. VALUATION SCORES
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_valuation_scores (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                TEXT UNIQUE NOT NULL,
    ticker              TEXT NOT NULL,
    valuation_score     INTEGER DEFAULT 0,
    valuation_zone      TEXT,
    valuation_multiplier FLOAT DEFAULT 1.0,
    trailing_pe         FLOAT,
    price_to_book       FLOAT,
    ev_to_ebitda        FLOAT,
    sector_median_pe    FLOAT,
    dimension_scores    JSONB DEFAULT '{}',
    score_narrative     TEXT,
    scored_at           TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- 5d. BULK/BLOCK DEALS
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_bulk_deals (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ticker          TEXT NOT NULL,
    isin            TEXT,
    trade_date      DATE NOT NULL,
    deal_type       TEXT NOT NULL,        -- BULK | BLOCK
    client_name     TEXT NOT NULL,
    buy_sell        TEXT NOT NULL,         -- BUY | SELL
    quantity        BIGINT,
    price           FLOAT,
    value_cr        FLOAT,
    is_superstar    BOOLEAN DEFAULT false,
    superstar_name  TEXT,
    is_institutional BOOLEAN DEFAULT false,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(ticker, trade_date, client_name, buy_sell)
);

-- ─────────────────────────────────────────────────────────────
-- 5e. SHAREHOLDING PATTERNS
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_shareholding_patterns (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    quarter         TEXT NOT NULL,         -- Q3FY26
    promoter_pct    FLOAT,
    fii_pct         FLOAT,
    dii_pct         FLOAT,
    mf_pct          FLOAT,
    insurance_pct   FLOAT,
    public_pct      FLOAT,
    notable_holders JSONB DEFAULT '[]',
    promoter_delta  FLOAT DEFAULT 0,
    fii_delta       FLOAT DEFAULT 0,
    mf_delta        FLOAT DEFAULT 0,
    dii_delta       FLOAT DEFAULT 0,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(isin, quarter)
);

-- ─────────────────────────────────────────────────────────────
-- 5f. SMART MONEY SCORES
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_smart_money_scores (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                TEXT UNIQUE NOT NULL,
    ticker              TEXT NOT NULL,
    smart_money_score   INTEGER DEFAULT 0,
    signals             JSONB DEFAULT '[]',
    signals_firing      INTEGER DEFAULT 0,
    superstar_entries   JSONB DEFAULT '[]',
    superstar_exits     JSONB DEFAULT '[]',
    mf_delta            FLOAT,
    fii_delta           FLOAT,
    score_narrative     TEXT,
    scored_at           TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- 5g. DEGRADATION FLAGS
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_degradation_flags (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    isin                TEXT UNIQUE NOT NULL,
    ticker              TEXT NOT NULL,
    degradation_score   INTEGER DEFAULT 0,
    is_degrading        BOOLEAN DEFAULT false,
    red_flags           JSONB DEFAULT '[]',
    flags_firing        INTEGER DEFAULT 0,
    score_narrative     TEXT,
    scored_at           TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────
-- 6. JOB MONITORING
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS india_job_runs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_name        TEXT NOT NULL,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    status          TEXT DEFAULT 'running',
    records_processed INTEGER DEFAULT 0,
    claude_calls_made INTEGER DEFAULT 0,
    cost_usd        FLOAT DEFAULT 0,
    error_msg       TEXT,
    details         JSONB DEFAULT '{}'
);

-- ─────────────────────────────────────────────────────────────
-- INDEXES
-- ─────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_promoter_signals_isin_date
    ON india_promoter_signals(isin, transaction_date DESC);

CREATE INDEX IF NOT EXISTS idx_promoter_signals_type_date
    ON india_promoter_signals(signal_type, transaction_date DESC);

CREATE INDEX IF NOT EXISTS idx_promoter_signals_ticker
    ON india_promoter_signals(ticker);

CREATE INDEX IF NOT EXISTS idx_financials_isin_type
    ON india_financials_history(isin, period_type, period_end DESC);

CREATE INDEX IF NOT EXISTS idx_hidden_gems_tier
    ON india_hidden_gems(conviction_tier, final_score DESC);

CREATE INDEX IF NOT EXISTS idx_companies_tier
    ON india_companies(market_cap_tier, market_cap_cr DESC);

CREATE INDEX IF NOT EXISTS idx_corporate_filings_isin_date
    ON india_corporate_filings(isin, sort_date DESC);

CREATE INDEX IF NOT EXISTS idx_corporate_filings_bucket
    ON india_corporate_filings(category_bucket, signal_priority);

CREATE INDEX IF NOT EXISTS idx_corporate_filings_unanalysed
    ON india_corporate_filings(is_analysed) WHERE is_analysed = false;

CREATE INDEX IF NOT EXISTS idx_valuation_scores_zone
    ON india_valuation_scores(valuation_zone, valuation_score DESC);

CREATE INDEX IF NOT EXISTS idx_bulk_deals_ticker_date
    ON india_bulk_deals(ticker, trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_bulk_deals_superstar
    ON india_bulk_deals(is_superstar, trade_date DESC) WHERE is_superstar = true;

CREATE INDEX IF NOT EXISTS idx_shareholding_isin_quarter
    ON india_shareholding_patterns(isin, quarter DESC);

CREATE INDEX IF NOT EXISTS idx_smart_money_scores_isin
    ON india_smart_money_scores(isin);

CREATE INDEX IF NOT EXISTS idx_degradation_flags_degrading
    ON india_degradation_flags(is_degrading, degradation_score);

CREATE INDEX IF NOT EXISTS idx_hidden_gems_degrading
    ON india_hidden_gems(is_degrading) WHERE is_degrading = true;

-- ─────────────────────────────────────────────────────────────
-- DONE — Verify with:
-- SELECT table_name FROM information_schema.tables
-- WHERE table_name LIKE 'india_%' ORDER BY table_name;
-- ─────────────────────────────────────────────────────────────
