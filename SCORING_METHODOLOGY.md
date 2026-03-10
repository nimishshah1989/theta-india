# JIP Horizon India -- Scoring Methodology

**Version:** 2026-Q1 | **Last Updated:** 2026-03-10

A quantitative hidden gem stock screener for Indian mid/small-cap markets. This document describes the complete scoring methodology: 5 signal layers, 3 modifiers, composite scoring with piecewise-linear rescaling, convergence bonuses, and conviction tier assignment.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Layer 1: Promoter Insider Trading (30%)](#2-layer-1-promoter-insider-trading-30-weight)
3. [Layer 2: Operating Leverage Inflection (30%)](#3-layer-2-operating-leverage-inflection-30-weight)
4. [Layer 3: Corporate Intelligence (25%)](#4-layer-3-corporate-intelligence-25-weight)
5. [Layer 4: Policy Tailwind (10%)](#5-layer-4-policy-tailwind-10-weight)
6. [Layer 5: Quality Emergence (5%)](#6-layer-5-quality-emergence-5-weight)
7. [Modifier 1: Valuation Gate (0.75x -- 1.15x)](#7-modifier-1-valuation-gate-075x-to-115x-multiplicative)
8. [Modifier 2: Smart Money Bonus (-10 to +15)](#8-modifier-2-smart-money-bonus--10-to-15-additive)
9. [Modifier 3: Degradation Penalty (-30 to 0)](#9-modifier-3-degradation-penalty--30-to-0-subtractive)
10. [Composite Scoring](#10-composite-scoring)
11. [Worked Example](#11-worked-example)
12. [Data Sources and Cost](#12-data-sources-and-cost)

---

## 1. Overview

### Architecture

The system identifies undervalued, undiscovered companies using **5 signal layers** combined into a weighted base score, then adjusted by **3 modifiers** that act as gates, bonuses, and penalties.

```
                    +---------------------------+
                    |   5 Signal Layers          |
                    |   (rescaled, weighted avg) |
                    +---------------------------+
                                |
                        base_composite
                                |
              +-----------------+-----------------+
              |                 |                 |
        Valuation Gate    Smart Money       Degradation
        (multiplicative)    (additive)     (subtractive)
         0.75x - 1.15x    -10 to +15       -30 to 0
              |                 |                 |
              +-----------------+-----------------+
                                |
                    final = clamp(base * val_mult
                           + smart_money + degradation,
                           0, 100)
                                |
                       Conviction Tier
```

### Final Formula

```
final_score = clamp(base_composite * valuation_multiplier + smart_money_bonus + degradation_penalty, 0, 100)
```

Where `base_composite` is a weighted average of rescaled layer scores with a convergence bonus applied.

### Conviction Tiers

| Tier | Threshold | Meaning |
|------|-----------|---------|
| **HIGHEST** | >= 70 | Strong multi-signal convergence across layers and modifiers |
| **HIGH** | >= 58 | Multiple layers firing with favorable valuation |
| **MEDIUM** | >= 45 | Emerging signals, worth monitoring closely |
| **WATCH** | >= 30 | Early-stage signals detected, not yet actionable |
| BELOW_THRESHOLD | < 30 | Insufficient signal strength |

Claude AI thesis synthesis is triggered **only** for HIGH and HIGHEST tier companies to control cost (~$0.005 per synthesis).

---

## 2. Layer 1: Promoter Insider Trading (30% Weight)

**File:** `india_alpha/signals/promoter_scorer.py`
**Reads from:** `india_promoter_signals` (rolling 12-month window)
**Writes to:** `india_promoter_summary`
**Cost:** Free (pure Python)

### Rationale

Promoter open market buying is the single strongest signal of insider conviction in Indian markets. Unlike institutional flows, promoters have material non-public information about their own company's trajectory. This layer captures buying patterns, warrant exercises, creeping acquisitions, and pledge changes.

### Signal Types and Base Points

| Signal Type | Base Points | Per-Type Cap | Description |
|-------------|-------------|--------------|-------------|
| `open_market_buy` | +9 | +25 | Strongest signal -- promoter deploying personal capital |
| `warrant_allotment` | +7 | +20 | Private valuation signal at premium pricing |
| `creeping_acquisition` | +7 | +20 | Systematic accumulation, increasing control |
| `pledge_decrease` | +6 | +15 | Financial health improving, stress reducing |
| `preferential_allotment` | +3 | -- | Positive but dilutive, moderate signal |
| `esop_exercise` | +2 | -- | Routine compensation event |
| `off_market` | +1 | -- | Transfer between related parties |
| `open_market_sell` | -5 | -20 | Negative -- promoter reducing exposure |
| `pledge_increase` | -8 | -20 | Stress signal -- promoter pledging shares for loans |

### Amplifiers (Open Market Buy Only)

These multipliers apply to individual `open_market_buy` transactions before the per-type cap:

| Condition | Multiplier | Rationale |
|-----------|------------|-----------|
| Transaction value >= 2 Cr | 1.3x | Meaningful capital commitment |
| Transaction value >= 5 Cr | Additional 1.2x | Large position sizing, high conviction |
| 5+ buy transactions in 12m | 1.2x | Consistent accumulation pattern |

Multipliers stack. A 6 Cr buy with 5+ prior buys scores: `9 * 1.3 * 1.2 * 1.2 = 16.85` (capped at 25 total for that signal type).

### Pledge Decrease Amplifier

| Condition | Multiplier | Rationale |
|-----------|------------|-----------|
| Post-transaction pledge < 2% | 1.5x | Pledge going to zero is a major positive catalyst |

### Pledge Trend Classification

Determined from the presence of pledge signals in the 12-month window:

| Trend | Condition |
|-------|-----------|
| **Falling** | Pledge decrease signals present, no pledge increase |
| **Rising** | Pledge increase signals present, no pledge decrease |
| **Volatile** | Both pledge increase and decrease signals present |
| **Stable** | No pledge-related signals |

### Score Computation

1. For each signal in the 12-month window, compute `contribution = base_points * amplifier`
2. Sum contributions per signal type, capping each at its per-type cap
3. Sum all type totals to get `raw_score`
4. `final_score = clamp(raw_score, 0, 100)`

### Output Fields

| Field | Type | Description |
|-------|------|-------------|
| `promoter_signal_score` | int (0-100) | Composite promoter score |
| `score_narrative` | string | Human-readable summary |
| `highest_conviction_signal` | string | Single most informative signal |
| `open_market_buying_cr_12m` | float | Total buy value in crores |
| `open_market_selling_cr_12m` | float | Total sell value in crores |
| `net_buying_cr_12m` | float | Buy minus sell |
| `buy_transaction_count_12m` | int | Number of buy transactions |
| `warrant_issued_12m` | bool | Whether warrants were issued |
| `creeping_acq_active` | bool | Whether creeping acquisition detected |
| `pledge_trend` | string | falling / rising / volatile / stable |

---

## 3. Layer 2: Operating Leverage Inflection (30% Weight)

**File:** `india_alpha/signals/operating_leverage.py`
**Reads from:** `india_financials_history` (annual periods, 5-year window, newest first)
**Writes to:** `india_operating_leverage_scores`
**Cost:** Free (pure Python)

### Rationale

Operating leverage inflection precedes earnings surprises. When a company's fixed cost base starts getting leveraged over rapidly growing revenue, EBITDA margin expands non-linearly. Detecting this inflection point early -- before analyst coverage catches up -- is the core alpha of this layer.

### Six Signals

#### Signal 1: Debt to Net Cash Transition (+25 or +14)

| Condition | Points | Label |
|-----------|--------|-------|
| Net debt (previous) > 5 Cr AND net debt (current) < 0 | +25 | `debt_to_net_cash` |
| Net debt reduced by > 60% (current < previous * 0.4) | +14 | `rapid_debt_reduction` |

Net debt = total_debt - cash. Crossing zero eliminates interest cost entirely, flowing savings directly to PAT.

#### Signal 2: EBITDA Margin Structural Expansion (+20, +12, or +6)

| Condition | Points | Label |
|-----------|--------|-------|
| 3-year margin expansion > 6pp AND 1-year > 1pp | +20 | `margin_structural_expansion` |
| 1-year margin expansion > 3pp | +12 | `margin_acceleration` |
| 1-year margin expansion > 1.5pp | +6 | (minor, no label) |

#### Signal 3: ROCE Inflection (+15 or +8)

| Condition | Points | Label |
|-----------|--------|-------|
| ROCE > 18% AND ROCE > (3-year-ago ROCE + 6pp) | +15 | `roce_inflection` |
| ROCE > (previous year + 3pp) AND ROCE > 12% | +8 | (improvement, no label) |

#### Signal 4: Revenue CAGR (+15, +8, or +4)

Computed as 3-year compound annual growth rate: `CAGR = (rev_now / rev_3yr)^(1/3) - 1`

| Condition | Points | Label |
|-----------|--------|-------|
| CAGR > 22% | +15 | `revenue_hypergrowth` |
| CAGR 14-22% | +8 | (growth, no label) |
| CAGR 8-14% | +4 | (base growth, no label) |

#### Signal 5: Receivables Compression (+12 or +6)

| Condition | Points | Label |
|-----------|--------|-------|
| Debtor days decreased > 15 days YoY | +12 | `receivables_compression` |
| Debtor days decreased > 8 days YoY | +6 | (tightening, no label) |

#### Signal 6: Equity Value Creation (+10)

| Condition | Points | Label |
|-----------|--------|-------|
| Net worth grew > 80% over 3 years (current > oldest * 1.8) | +10 | `equity_value_creation` |

### Inflection Candidate

A company is marked as `is_inflection_candidate = True` when:
- `ol_score >= 35` **AND**
- `signals_firing >= 2` (at least 2 named signals triggered)

### Score Range

`final_score = clamp(raw_sum, 0, 100)`. Maximum theoretical score: 25 + 20 + 15 + 15 + 12 + 10 = **97**.

---

## 4. Layer 3: Corporate Intelligence (25% Weight)

**File:** `india_alpha/signals/corporate_intelligence_scorer.py`
**Reads from:** `india_corporate_filings` (NSE corporate announcements)
**Writes to:** `india_corporate_intelligence_scores`
**Cost:** ~$0.01-0.02 per Claude call, max 3 per company, 40 per pipeline run

### Rationale

NSE corporate filings contain rich forward-looking information -- credit ratings, order wins, capacity expansions, management changes, and M&A. This layer extracts investment signals from filings using two modes: free Python rules for most filings, and cost-controlled Claude analysis for high-priority PDFs with extracted text.

### Mode A: Python Rules Scoring (Free)

Every filing is scored by matching its `category` field against a rules map:

| Category | Scoring Function | Score Range | Examples |
|----------|-----------------|-------------|----------|
| Credit rating | `_score_credit_rating` | -10 to +15 | Upgrade +15, Reaffirm +5, Downgrade -10, Watch -5 |
| Dividend | `_score_dividend` | +5 to +10 | Special/interim +10, Final +5 |
| Appointment/Cessation/Resignation | `_score_management_change` | -8 to +5 | CFO/CEO/MD resignation -8, Director cessation -4, KMP appointment +5/+3 |
| Auditor/Change in Auditor | `_score_auditor_change` | -12 to -2 | Mid-term change -12, Routine rotation -2 |
| Acquisition/Amalgamation/Merger | `_score_acquisition` | +6 to +12 | Strategic/100%/majority +12, Subsidiary/JV +8, Stake +8 |
| Press release | `_score_press_release` | -5 to +10 | Order win +10, Partnership +8, Expansion +8, Litigation -3, Adverse event -5 |
| Takeover/Regulation 29 | `_score_sebi_takeover` | -5 to +8 | Stake increase +8, Disposal -5 |
| Buyback | `_score_buyback` | +10 | Management signals undervaluation |
| Bonus/Split | `_score_bonus_split` | +3 to +5 | Bonus +5, Split +3 |
| ESOP/ESOS | `_score_esop` | +2 | Routine compensation |
| Other (uncategorized) | default | +1 | Filing recorded |

### Mode B: Claude Analysis (Cost-Controlled)

Triggered only for filings meeting ALL conditions:
- `signal_priority == "HIGH"`
- `is_text_extracted == True`
- `word_count >= 200`
- Maximum 3 Claude calls per company
- Maximum 40 Claude calls per pipeline run
- Subject to CostTracker budget check

Two prompt templates are used based on the filing's `category_bucket`:

#### Earnings Strategy Prompt (EARNINGS_ANALYSIS_PROMPT)

Scores on 4 dimensions plus a deduction:

| Dimension | Max Score | Description |
|-----------|-----------|-------------|
| Tone Score | 0-25 | Management tone: bullish/confident/neutral/cautious/defensive |
| Forward Score | 0-25 | Strength and specificity of forward-looking statements |
| Quant Score | 0-25 | Quantitative commitments (revenue/margin/capex targets) |
| Hidden Insight Score | 0-10 | What most analysts would miss |
| Red Flag Deduction | 0 to -15 | Concerns identified |
| **Total** | **0-100** | |

#### Capital Action Prompt (CAPITAL_ACTION_PROMPT)

Scores on 4 dimensions plus a deduction:

| Dimension | Max Score | Description |
|-----------|-----------|-------------|
| Strategic Score | 0-25 | Strategic value of the capital action |
| Financial Score | 0-25 | Quantified financial impact |
| Execution Score | 0-25 | Execution risk assessment (low risk = high score) |
| Hidden Insight Score | 0-10 | What most analysts would miss |
| Red Flag Deduction | 0 to -15 | Concerns identified |
| **Total** | **0-100** | |

### Recency Multipliers

All filing scores (both Python and Claude) are weighted by filing age:

| Filing Age | Multiplier | Rationale |
|------------|------------|-----------|
| <= 90 days | 1.0x | Full weight -- recent and relevant |
| <= 180 days | 0.8x | Still material, slight decay |
| <= 365 days | 0.5x | Aging information |
| > 365 days | 0.3x | Stale but provides context |

### Bucket Aggregation

Filing scores are grouped into three buckets with weighted combination:

| Bucket | Weight | Content |
|--------|--------|---------|
| **Earnings Strategy** | 55% | Concall transcripts, results, forward guidance |
| **Capital Action** | 30% | Acquisitions, buybacks, expansions, M&A |
| **Governance** | 15% | Management changes, auditor changes, compliance |

Each bucket is computed as `sum(score * recency_multiplier)`, clamped to 0-100.
Claude scores are normalized before aggregation: `(claude_score / 100) * 25 * recency_multiplier`.

Final: `corporate_intelligence_score = earnings * 0.55 + capital * 0.30 + governance * 0.15`, clamped to 0-100.

---

## 5. Layer 4: Policy Tailwind (10% Weight)

**File:** `india_alpha/signals/policy_scorer.py`
**Reads from:** `india_companies` (sector, industry fields) + `india_alpha/data/policy_registry.json`
**Writes to:** `india_policy_scores`
**Cost:** Free (pure Python)

### Rationale

Government policy creates structural demand tailwinds for specific sectors. Companies aligned with active policies (PLI schemes, defense indigenization, infrastructure push) benefit from guaranteed demand floors, subsidies, and import substitution dynamics. This layer identifies companies sitting in the path of government spending.

### Active Policies (15 Total)

| Policy ID | Policy Name | Impact | Points |
|-----------|-------------|--------|--------|
| `pli_electronics` | PLI Scheme -- IT Hardware & Electronics | HIGH | 15 |
| `pli_pharma` | PLI Scheme -- Pharmaceuticals & API | HIGH | 15 |
| `pli_auto` | PLI Scheme -- Auto & Auto Components | HIGH | 15 |
| `pli_textiles` | PLI Scheme -- Textiles (MMF & Technical) | MEDIUM | 10 |
| `pli_food` | PLI Scheme -- Food Processing | MEDIUM | 10 |
| `pli_steel` | PLI Scheme -- Specialty Steel | MEDIUM | 10 |
| `pli_solar` | PLI Scheme -- Solar PV Manufacturing | HIGH | 15 |
| `defense_indigenization` | Defense Indigenization & Positive List | HIGH | 15 |
| `semiconductor_fab` | India Semiconductor Mission | HIGH | 15 |
| `green_hydrogen` | National Green Hydrogen Mission | MEDIUM | 10 |
| `pm_gati_shakti` | PM Gati Shakti -- Infrastructure Push | HIGH | 15 |
| `digital_india` | Digital India & IT Modernization | MEDIUM | 10 |
| `fame_ev` | FAME III / EV Ecosystem Support | HIGH | 15 |
| `pm_awas_yojana` | PM Awas Yojana (Urban & Rural Housing) | MEDIUM | 10 |
| `china_plus_one` | China+1 Manufacturing Shift | MEDIUM | 10 |

### Impact Score Mapping

| Impact Level | Points |
|-------------|--------|
| HIGH | 15 |
| MEDIUM | 10 |
| LOW | 5 |

### Matching Logic

A company matches a policy if **either** condition is met:

1. **Sector Match:** Company `sector` field contains (or is contained in) any of the policy's `beneficiary_sectors` (case-insensitive)
2. **Keyword Match:** Any of the policy's `beneficiary_keywords` appears in the combined `sector + industry` string (case-insensitive)

Each policy has its own keyword list. For example, `defense_indigenization` matches on: "defense", "defence", "military", "aerospace", "ammunition", "naval", "missile", "radar".

### Score Computation

```
raw_score = sum(points for each matching policy)
policy_score = clamp(raw_score, 0, 100)
```

A company in defense electronics matching both `defense_indigenization` (15) and `pli_electronics` (15) would score 30. A company matching 7+ policies is theoretically possible but practically capped at 100.

---

## 6. Layer 5: Quality Emergence (5% Weight)

**File:** `india_alpha/signals/quality_scorer.py`
**Reads from:** `india_financials_history` (annual periods, 3+ years required, newest first)
**Writes to:** `india_quality_scores`
**Cost:** Free (pure Python)

### Rationale

Quality emergence detects the phase-change moment when a company transitions from mediocre to fundamentally sound. Unlike Layer 2 (operating leverage) which focuses on margin inflection, this layer looks for sustained, multi-dimensional quality improvement -- the hallmarks of a company building a durable competitive advantage.

### Six Signals

#### Signal 1: ROE Breakout (+20)

| Condition | Points | Label |
|-----------|--------|-------|
| Current ROE >= 15% AND prior-year average ROE < 13% | +20 | `roe_breakout` |

The 15% threshold is significant: it represents the point where equity returns meaningfully exceed cost of capital for most Indian companies.

#### Signal 2: ROCE Consistency (+18)

| Condition | Points | Label |
|-----------|--------|-------|
| ROCE > 15% for 2+ consecutive years AND current ROCE > previous year ROCE | +18 | `roce_consistency` |

Sustained high ROCE with improvement signals competitive moat building.

#### Signal 3: Margin Expansion Streak (+18 or +10)

| Condition | Points | Label |
|-----------|--------|-------|
| EBITDA margin expanding 3+ consecutive years AND total expansion > 3pp | +18 | `margin_expansion_streak` |
| EBITDA margin expanding 2+ consecutive years AND total expansion > 2pp | +10 | (minor, no label) |

Streak is measured newest-to-oldest: each year's margin must exceed the next year's (in descending order).

#### Signal 4: Deleveraging (+16)

| Condition | Points | Label |
|-----------|--------|-------|
| Current D/E < 0.5x AND historical peak D/E > 0.8x | +16 | `deleveraging` |

D/E = total_debt / net_worth. Crossing below 0.5 from above 0.8 represents a balance sheet transformation.

#### Signal 5: Working Capital Tightening (+14 or +7)

Working capital cycle = debtor_days + inventory_days.

| Condition | Points | Label |
|-----------|--------|-------|
| WC cycle declining 3+ consecutive years | +14 | `working_capital_tightening` |
| WC cycle declining 2+ consecutive years | +7 | (minor, no label) |

#### Signal 6: Earnings Quality (+14)

| Condition | Points | Label |
|-----------|--------|-------|
| PAT margin expanding (current > oldest + 1pp) AND revenue CAGR > 10% | +14 | `earnings_quality` |

Profitable growth without margin sacrifice -- the hallmark of high-quality earnings.

### Convergence Multiplier

| Condition | Multiplier |
|-----------|------------|
| 4+ named signals firing simultaneously | 1.15x |

When quality improvement is happening across 4+ dimensions simultaneously, it is reinforcing and self-sustaining, warranting a bonus multiplier.

### Score Range

`final_score = clamp(raw_sum * multiplier, 0, 100)`. Maximum theoretical: (20 + 18 + 18 + 16 + 14 + 14) * 1.15 = **115**, clamped to **100**.

---

## 7. Modifier 1: Valuation Gate (0.75x to 1.15x, Multiplicative)

**File:** `india_alpha/signals/valuation_scorer.py`
**Reads from:** `india_companies` (yfinance-sourced valuation data)
**Writes to:** `india_valuation_scores`
**Cost:** Free (pure Python)

### Rationale

A high-quality company at the wrong price is still a bad investment. The valuation gate acts as a multiplicative gate on the composite score -- cheap stocks get amplified, expensive stocks get penalized. This prevents the screener from surfacing fundamentally strong companies that are already fully priced.

### Five Valuation Dimensions

#### Dimension 1: PE vs Sector Median (25 points max)

Sector median PE is computed across the full universe (minimum 3 companies per sector required). Ratio = company trailing PE / sector median PE.

| PE / Sector Median | Points |
|---------------------|--------|
| < 0.50x | 25 |
| 0.50x -- 0.75x | 18 |
| 0.75x -- 1.00x | 12 |
| 1.00x -- 1.50x | 6 |
| > 1.50x | 0 |

*Requires both valid positive trailing PE and sector median PE. If sector has < 3 companies, this dimension is skipped.*

#### Dimension 2: Absolute PE (25 points max)

| Trailing PE | Points |
|-------------|--------|
| < 8x | 25 |
| 8x -- 15x | 18 |
| 15x -- 25x | 10 |
| 25x -- 40x | 3 |
| > 40x | 0 |
| Negative (loss-making) | 0 |

#### Dimension 3: Price-to-Book (15 points max)

| P/B Ratio | Points |
|-----------|--------|
| < 1.0x | 15 |
| 1.0x -- 2.0x | 10 |
| 2.0x -- 4.0x | 5 |
| 4.0x -- 6.0x | 2 |
| > 6.0x | 0 |

#### Dimension 4: EV/EBITDA (20 points max)

| EV/EBITDA | Points |
|-----------|--------|
| < 6x | 20 |
| 6x -- 10x | 14 |
| 10x -- 15x | 8 |
| 15x -- 20x | 3 |
| > 20x | 0 |

#### Dimension 5: 52-Week Position (15 points max)

Position is calculated as: `((current_price - 52w_low) / (52w_high - 52w_low)) * 100`

| 52-Week Position | Points |
|------------------|--------|
| 0-20% (near low) | 15 |
| 20-40% | 10 |
| 40-60% | 6 |
| 60-80% | 3 |
| 80-100% (near high) | 0 |

**200-DMA Bonus:** If `current_price < 200-day moving average`, add +3 points to this dimension.

### Missing Data Handling

- If **no valuation data** is available at all (0 dimensions scored), the company defaults to `valuation_score = 35`, `zone = FAIR`, `multiplier = 1.00x`.
- If **2 or fewer dimensions** have data, missing dimensions are imputed at 60% of the average scored proportion across available dimensions.

### Valuation Zones and Multipliers

| Zone | Score Range | Multiplier | Effect on Composite |
|------|------------|------------|---------------------|
| **DEEP_VALUE** | >= 75 | 1.15x | Amplifies base score by 15% |
| **CHEAP** | 55 -- 74 | 1.08x | Amplifies base score by 8% |
| **FAIR** | 35 -- 54 | 1.00x | No effect |
| **EXPENSIVE** | 20 -- 34 | 0.90x | Penalizes base score by 10% |
| **OVERVALUED** | < 20 | 0.75x | Penalizes base score by 25% |

### Total Score

```
valuation_score = clamp(sum(all 5 dimension scores), 0, 100)
```

Maximum theoretical: 25 + 25 + 15 + 20 + 15 + 3 (DMA bonus) = **103**, clamped to **100**.

---

## 8. Modifier 2: Smart Money Bonus (-10 to +15, Additive)

**File:** `india_alpha/signals/smart_money_scorer.py`
**Reads from:** `india_shareholding_patterns` (latest 2 quarters) + `india_bulk_deals` (last 30 days)
**Writes to:** `india_smart_money_scores`
**Cost:** Free (pure Python)

### Rationale

Smart money -- superstar investors, mutual funds, FIIs, and institutional bulk buyers -- provides confirmation (or contradiction) of the quantitative thesis. A superstar entering a stock that already scores high on fundamentals is a powerful convergence signal. Conversely, smart money exiting suggests information asymmetry we should respect.

### 24 Tracked Superstar Investors

| # | Investor | Known Aliases |
|---|----------|---------------|
| 1 | Ashish Kacholia | Lucky Securities |
| 2 | Vijay Kedia | Kedia Securities |
| 3 | Dolly Khanna | Rajiv Khanna |
| 4 | Mohnish Pabrai | Dalal Street LLC |
| 5 | Radhakishan Damani | Bright Star Investments, Avenue Supermarts |
| 6 | RARE Enterprises (Jhunjhunwala Legacy) | Rekha Jhunjhunwala |
| 7 | Mukul Agrawal | Mukul Mahavir Prasad Agrawal |
| 8 | Sunil Singhania | Abakkus Growth Fund |
| 9 | Porinju Veliyath | Equity Intelligence |
| 10 | Anil Kumar Goel | Goel Investments |
| 11 | Madhusudan Kela | MK Ventures |
| 12 | Nemish Shah | Shrevyas Investments |
| 13 | Ramesh Damani | -- |
| 14 | Shankar Sharma | First Global |
| 15 | Akash Bhanshali | Enam Holdings |
| 16 | Sanjay Bakshi | -- |
| 17 | Basant Maheshwari | Basant Maheshwari Wealth Advisers |
| 18 | Kenneth Andrade | Old Bridge Capital |
| 19 | Sameer Narayan | -- |
| 20 | Raamdeo Agrawal | Motilal Oswal |
| 21 | Vallabh Bhanshali | -- |
| 22 | S Naren | Sankaran Naren |
| 23 | Sumeet Nagar | Malabar Investments |
| 24 | Saurabh Mukherjea | Marcellus Investment |
| 25 | Amit Jeswani | Stallion Asset |

*Note: Matching uses the aliases list and superstar flags from BSE shareholding data.*

### Institutional Keywords for Bulk Deal Classification

Bulk deals are classified as `is_institutional` if the client name matches any of 40+ institutional keywords including: mutual fund, insurance, pension, bank, FII, FPI, securities, capital, asset management, AMC, hedge fund, and specific institutional names (Goldman Sachs, Morgan Stanley, JP Morgan, LIC of India, SBI Mutual, HDFC Mutual, etc.).

### Nine Signals

| # | Signal | Points | Condition |
|---|--------|--------|-----------|
| 1 | Superstar new entry | +10 | Name in current quarter shareholding but not in previous quarter |
| 2 | Superstar increased | +6 | Same superstar, higher % holding in current vs previous quarter |
| 3 | Superstar exited | -8 | Name in previous quarter but not in current quarter |
| 4 | MF accumulation (strong) | +6 | Mutual fund holding increased > 1.0pp QoQ |
| 5 | MF accumulation (moderate) | +3 | MF holding increased > 0.5pp but <= 1.0pp QoQ |
| 6 | MF exit | -4 | MF holding decreased > 1.0pp QoQ |
| 7 | FII accumulation | +4 | FII holding increased > 0.5pp QoQ |
| 8 | Institutional bulk buy | +5 | Institutional BUY deal in last 30 days |
| 9 | Institutional bulk sell | -5 | Institutional SELL deal in last 30 days |

Signals 4 and 5 are mutually exclusive (only the higher one fires). MF/FII deltas are computed from either pre-computed `mf_delta`/`fii_delta` fields or by subtracting previous quarter percentages from current.

### Score Computation

```
raw_total = sum(points for all fired signals)
smart_money_score = clamp(raw_total, -10, +15)
```

The asymmetric cap (+15 / -10) reflects the design philosophy: smart money confirmation adds value, but exits are weighted less because institutional selling can be driven by portfolio rebalancing rather than fundamental concerns.

---

## 9. Modifier 3: Degradation Penalty (-30 to 0, Subtractive)

**File:** `india_alpha/signals/degradation_monitor.py`
**Reads from:** All existing tables (cross-table scan, no new data fetching)
**Writes to:** `india_degradation_flags`
**Cost:** Free (pure Python)

### Rationale

A company can score well on historical signals while simultaneously deteriorating in real-time. The degradation monitor cross-references insider activity, financial trends, corporate filings, and price data to detect active deterioration. It serves as a safety net against stale-signal false positives.

### Ten Red Flags

| # | Red Flag | Penalty | Data Source | Lookback | Condition |
|---|----------|---------|-------------|----------|-----------|
| 1 | Net insider selling | -8 | `india_promoter_signals` | 90 days | Sell value > buy value by > 0.5 Cr |
| 2 | Pledge increasing | -6 | `india_promoter_signals` | 90 days | Most recent pledge_increase has higher post-pct than pre-pct |
| 3 | Multiple insiders selling | -5 | `india_promoter_signals` | 90 days | >= 3 distinct person names with open_market_sell |
| 4 | EBITDA margin declining | -6 | `india_financials_history` (quarterly) | 3+ quarters | 2+ consecutive quarters of margin decline |
| 5 | Revenue declining YoY | -5 | `india_financials_history` (annual) | 2 years | Latest annual revenue < previous annual revenue |
| 6 | Leverage up, margins down | -5 | `india_financials_history` + `india_companies` | 2 years | D/E ratio rose AND EBITDA margin fell simultaneously |
| 7 | Auditor change (mid-term) | -8 | `india_corporate_filings` | 12 months | Filing with "auditor" category or subject containing auditor change keywords |
| 8 | Key management resignation | -6 | `india_corporate_filings` | 12 months | Filing with "resignation" AND any of: CFO, CEO, Chief Financial, Chief Executive, Managing Director |
| 9 | Credit rating downgrade | -5 | `india_corporate_filings` | 12 months | Filing with "credit rating" AND "downgrade" in subject |
| 10 | Price below 200-DMA | -3 | `india_companies` | Current | Current price > 15% below 200-day moving average |

*Flags 7, 8, and 9 are each triggered at most once (first match only, to avoid over-penalizing).*

### Degradation Assessment

```
raw_score = sum(penalty for each triggered red flag)
degradation_score = max(-30, raw_score)     # Floor at -30
is_degrading = (degradation_score <= -15)   # True if significant deterioration
```

Maximum theoretical penalty: -8 + -6 + -5 + -6 + -5 + -5 + -8 + -6 + -5 + -3 = **-57**, floored to **-30**.

When `is_degrading` is True, the gem_scorer may optionally downgrade the conviction tier by one level (e.g., HIGH becomes MEDIUM).

---

## 10. Composite Scoring

**File:** `india_alpha/processing/gem_scorer.py`
**Reads from:** All 8 score tables (5 layers + 3 modifiers) + `india_companies`
**Writes to:** `india_hidden_gems`
**Cost:** ~$0.005 per Claude thesis synthesis (HIGH/HIGHEST only)

### Step 1: Layer Score Rescaling

Raw layer scores tend to cluster in the 10-30 range. To spread them meaningfully across the 0-100 range for weighted averaging, each layer is rescaled using a **piecewise-linear breakpoint table**.

#### Rescaling Breakpoint Tables

**Promoter Layer:**

| Raw Score | Rescaled Score |
|-----------|----------------|
| 0 | 0 |
| 10 | 25 |
| 20 | 50 |
| 30 | 70 |
| 50 | 85 |
| 75 | 95 |
| 100 | 100 |

**Operating Leverage Layer:**

| Raw Score | Rescaled Score |
|-----------|----------------|
| 0 | 0 |
| 10 | 20 |
| 25 | 50 |
| 40 | 70 |
| 55 | 85 |
| 75 | 95 |
| 100 | 100 |

**Corporate Intelligence (Concall) Layer:**

| Raw Score | Rescaled Score |
|-----------|----------------|
| 0 | 0 |
| 8 | 20 |
| 18 | 50 |
| 30 | 70 |
| 45 | 85 |
| 70 | 95 |
| 100 | 100 |

**Policy Layer:**

| Raw Score | Rescaled Score |
|-----------|----------------|
| 0 | 0 |
| 10 | 25 |
| 20 | 50 |
| 35 | 70 |
| 50 | 85 |
| 75 | 95 |
| 100 | 100 |

**Quality Layer:**

| Raw Score | Rescaled Score |
|-----------|----------------|
| 0 | 0 |
| 10 | 25 |
| 20 | 50 |
| 30 | 70 |
| 50 | 85 |
| 75 | 95 |
| 100 | 100 |

Intermediate values are linearly interpolated between breakpoints. For example, a raw promoter score of 15 is rescaled as:
- Falls between breakpoints (10, 25) and (20, 50)
- `t = (15 - 10) / (20 - 10) = 0.5`
- `rescaled = 25 + 0.5 * (50 - 25) = 37.5`

### Step 2: Dynamic Weighted Average

Only layers with data (raw score > 0) participate in the weighted average. Weights are renormalized to sum to 1.0 across active layers.

| Layer | Weight | Description |
|-------|--------|-------------|
| Promoter | 30% | Insider trading conviction |
| Operating Leverage | 30% | Earnings inflection potential |
| Corporate Intelligence | 25% | Filing-based forward signals |
| Policy Tailwind | 10% | Government spending alignment |
| Quality Emergence | 5% | Multi-dimensional quality improvement |

**Formula:**

```
active_weights = {layer: weight for layer, weight in SCORE_WEIGHTS if raw_score[layer] > 0}
total_active = sum(active_weights.values())

base_composite = sum(rescaled[layer] * weight for layer, weight in active_weights) / total_active
```

If a company has data for only promoter (30%) and OL (30%), the denominator is 0.60 -- effectively giving each active layer a higher absolute contribution.

### Step 3: Convergence Bonus

When multiple layers independently confirm the thesis (each producing a rescaled score >= 40), a convergence bonus is applied:

| Layers >= 40 Rescaled | Bonus Multiplier | Rationale |
|------------------------|-----------------|-----------|
| 4+ layers | 1.15x (15%) | Rare multi-dimensional convergence |
| 3 layers | 1.10x (10%) | Strong cross-layer confirmation |
| 2 layers | 1.06x (6%) | Meaningful two-signal convergence |
| 0-1 layers | 1.00x (0%) | Single-signal, no convergence |

```
if layers_with_rescaled_score_ge_40 >= 4:
    base_composite = min(100, base_composite * 1.15)
elif layers_with_rescaled_score_ge_40 >= 3:
    base_composite = min(100, base_composite * 1.10)
elif layers_with_rescaled_score_ge_40 >= 2:
    base_composite = min(100, base_composite * 1.06)
```

### Step 4: Apply Modifiers

```
final = clamp(base_composite * valuation_multiplier + smart_money_bonus + degradation_penalty, 0, 100)
```

| Modifier | Range | Type | When Applied |
|----------|-------|------|-------------|
| Valuation Gate | 0.75x -- 1.15x | Multiplicative | Always (defaults to 1.0 if no data) |
| Smart Money Bonus | -10 to +15 | Additive | Always (defaults to 0 if no data) |
| Degradation Penalty | -30 to 0 | Subtractive | Always (defaults to 0 if no data) |

### Step 5: Tier Assignment and Claude Synthesis

```
tier = HIGHEST if score >= 70
     | HIGH    if score >= 58
     | MEDIUM  if score >= 45
     | WATCH   if score >= 30
     | BELOW_THRESHOLD otherwise
```

**Claude thesis synthesis** runs only for HIGH and HIGHEST tiers. It generates:
- `gem_thesis`: 3-sentence investment thesis
- `key_catalyst`: Specific re-rating trigger
- `catalyst_timeline`: Expected quarter (e.g., Q2FY27)
- `catalyst_confidence`: high / medium / low
- `primary_risk`: Single biggest thesis-breaking risk
- `what_market_misses`: The specific classification error the market is making
- `entry_note`: Price/technical condition for optimal entry

### Additional Flags

| Flag | Condition | Purpose |
|------|-----------|---------|
| `is_below_institutional` | Market cap < 2,500 Cr | Below institutional radar |
| `is_pre_discovery` | Analyst count < 3 | Under-researched |
| `layers_firing` | Count of layers with rescaled >= 40 | Signal breadth indicator |

---

## 11. Worked Example

### Company: Hypothetical Precision Industries Ltd (HYPOTHETICAL)

A mid-cap auto components manufacturer with 1,800 Cr market cap and 1 analyst covering it.

---

#### Step 1: Individual Layer Scoring

**Layer 1 -- Promoter (raw: 22)**
- 3 open market buys totaling 3.5 Cr in last 12 months
- Buy 1: 1.5 Cr = 9 base points
- Buy 2: 2.0 Cr = 9 * 1.3 (>= 2 Cr) = 11.7 points
- Buy 3: 0.5 Cr = 9 base points (no buy count amplifier since < 5 buys)
- Sum of buy contributions: min(9 + 11.7 + 9, 25) = 25.0 (capped)
- No sells, no pledges
- Pledge trend: stable
- **promoter_signal_score = 25** (capped at cap of 25 from buys alone, some points from minor signals could bring to 25; here we use 22 for illustration where only 2 buys cleared the cap scenario differently)

*For this example, let us set the raw promoter score at 22.*

**Layer 2 -- Operating Leverage (raw: 41)**
- Revenue CAGR 24% over 3 years: +15 (`revenue_hypergrowth`)
- EBITDA margin expanded from 11% to 17.5% over 3 years (+6.5pp), +1.5pp last year: +20 (`margin_structural_expansion`)
- Debtor days improved from 72 to 63 (-9 days): +6 (tightening, below 15-day threshold for full signal)
- 2 named signals firing, OL score >= 35: `is_inflection_candidate = True`
- **ol_score = 41**

**Layer 3 -- Corporate Intelligence (raw: 18)**
- Python rules: Order win filing (+10 * 1.0 recency = 10), Credit rating reaffirm (+5 * 0.8 recency = 4), KMP appointment (+3 * 0.5 = 1.5)
- No Claude analysis triggered (no HIGH priority PDFs with sufficient text)
- Earnings strategy bucket: 10 + 4 = 14
- Governance bucket: 1.5
- Capital action bucket: 0
- Composite: 14 * 0.55 + 0 * 0.30 + 1.5 * 0.15 = 7.7 + 0 + 0.225 = ~8
- Wait -- let us recalculate for a more interesting example. Assume a richer set:
  - Order win (+10, 1.0x) = 10
  - Strategic acquisition (+12, 0.8x) = 9.6
  - Credit rating upgrade (+15, 1.0x) = 15
  - Earnings bucket sum: 10, Capital bucket sum: 9.6, Governance bucket sum: 15
  - Composite: min(100, 10) * 0.55 + min(100, 9.6) * 0.30 + min(100, 15) * 0.15 = 5.5 + 2.88 + 2.25 = 10.63
  - Rounded: **corporate_intelligence_score = 11**

*For cleaner math, let us set raw corporate intelligence at 18.*

**Layer 4 -- Policy (raw: 25)**
- Sector: "Auto Components" matches `pli_auto` (HIGH, 15 pts)
- Industry keyword "auto" also matches `fame_ev` (HIGH, 15 pts) -- but checking: "ev" keyword match depends on industry text. Assume "auto components manufacturing" matches `pli_auto` only.
- Additionally, "China+1" (MEDIUM, 10 pts) matches keyword "manufacturing" in industry.
- **policy_score = 25** (15 + 10)

**Layer 5 -- Quality (raw: 34)**
- ROE at 16.5%, prior-year average was 11.2%: +20 (`roe_breakout`)
- Earnings quality: PAT margin up 2.1pp, revenue CAGR 24%: +14 (`earnings_quality`)
- 2 signals firing (below 4 threshold for multiplier)
- **quality_score = 34**

---

#### Step 2: Rescaling

| Layer | Raw Score | Rescaling Calculation | Rescaled Score |
|-------|-----------|----------------------|----------------|
| Promoter | 22 | Between (20, 50) and (30, 70): t = (22-20)/(30-20) = 0.2, result = 50 + 0.2*20 | **54.0** |
| OL | 41 | Between (40, 70) and (55, 85): t = (41-40)/(55-40) = 0.067, result = 70 + 0.067*15 | **71.0** |
| Corp Intel | 18 | Between (8, 20) and (18, 50): t = (18-8)/(18-8) = 1.0, result = 20 + 1.0*30 | **50.0** |
| Policy | 25 | Between (20, 50) and (35, 70): t = (25-20)/(35-20) = 0.333, result = 50 + 0.333*20 | **56.7** |
| Quality | 34 | Between (30, 70) and (50, 85): t = (34-30)/(50-30) = 0.2, result = 70 + 0.2*15 | **73.0** |

---

#### Step 3: Dynamic Weighted Average

All 5 layers have data (raw > 0), so all weights participate. Total weight = 1.00.

```
base = (54.0 * 0.30) + (71.0 * 0.30) + (50.0 * 0.25) + (56.7 * 0.10) + (73.0 * 0.05)
     = 16.2 + 21.3 + 12.5 + 5.67 + 3.65
     = 59.32
```

---

#### Step 4: Convergence Bonus

Layers with rescaled >= 40:
- Promoter: 54.0 -- yes
- OL: 71.0 -- yes
- Corp Intel: 50.0 -- yes
- Policy: 56.7 -- yes
- Quality: 73.0 -- yes

**5 layers >= 40** --> 1.15x bonus (4+ threshold).

```
base_composite = min(100, 59.32 * 1.15) = min(100, 68.22) = 68.2
```

---

#### Step 5: Apply Modifiers

**Valuation Score:** The company has PE of 12x (sector median 22x), P/B 1.8x, EV/EBITDA 7.5x, trading at 35% of 52-week range.

| Dimension | Value | Points |
|-----------|-------|--------|
| PE vs Sector | 12/22 = 0.545 (< 0.75x) | 18 |
| Absolute PE | 12x (8-15 range) | 18 |
| P/B | 1.8x (1.0-2.0 range) | 10 |
| EV/EBITDA | 7.5x (6-10 range) | 14 |
| 52-Week Position | 35% (20-40% range) | 10 |
| **Total** | | **70** |

Valuation zone: **CHEAP** (55-74) --> multiplier = **1.08x**

**Smart Money:** Ashish Kacholia entered with 1.2% stake (new entry = +10). MF holding up 0.8pp QoQ (+3). No negative signals.
- raw = 10 + 3 = 13, capped at **+13**

**Degradation:** No red flags detected.
- **degradation_score = 0**

---

#### Step 6: Final Score

```
final = clamp(68.2 * 1.08 + 13 + 0, 0, 100)
      = clamp(73.66 + 13 + 0, 0, 100)
      = clamp(86.66, 0, 100)
      = 86.7
```

---

#### Step 7: Tier Assignment

`86.7 >= 70` --> **HIGHEST** conviction tier.

Claude thesis synthesis will be triggered for this company.

#### Summary

| Component | Value |
|-----------|-------|
| Promoter (raw / rescaled) | 22 / 54.0 |
| OL (raw / rescaled) | 41 / 71.0 |
| Corp Intel (raw / rescaled) | 18 / 50.0 |
| Policy (raw / rescaled) | 25 / 56.7 |
| Quality (raw / rescaled) | 34 / 73.0 |
| Weighted Average | 59.3 |
| Convergence Bonus | 1.15x (5 layers) |
| Base Composite | 68.2 |
| Valuation Multiplier | 1.08x (CHEAP) |
| Smart Money Bonus | +13 |
| Degradation Penalty | 0 |
| **Final Composite** | **86.7** |
| **Conviction Tier** | **HIGHEST** |
| Additional Flags | `is_below_institutional=True`, `is_pre_discovery=True`, `layers_firing=5` |

---

## 12. Data Sources and Cost

### Pipeline Steps and Data Flow

| Step | Name | Type | Data Source | Table Written | API Cost |
|------|------|------|-------------|---------------|----------|
| 1 | Universe Build | Fetcher | NSE CSV + yfinance | `india_companies` | Free |
| 2 | Screener.in Enrichment | Fetcher | Screener.in HTML | `india_companies`, `india_financials_history` | Free |
| 3 | BSE Insider Signals | Fetcher | BSE API | `india_promoter_signals` | Free |
| 4 | Promoter Scoring | Scorer | `india_promoter_signals` | `india_promoter_summary` | Free |
| 5 | Screener.in Financials | Fetcher | Screener.in HTML | `india_financials_history` | Free |
| 6 | OL Scoring | Scorer | `india_financials_history` | `india_operating_leverage_scores` | Free |
| 7 | Quality Scoring | Scorer | `india_financials_history` | `india_quality_scores` | Free |
| 8 | Policy Scoring | Scorer | `india_companies` + `policy_registry.json` | `india_policy_scores` | Free |
| 9 | NSE Corporate Filings | Fetcher | NSE Announcements API | `india_corporate_filings` | Free |
| 10 | Corporate Intelligence | Scorer | `india_corporate_filings` + Claude | `india_corporate_intelligence_scores` | ~$0.02/filing |
| 11 | Valuation Scoring | Scorer | `india_companies` (yfinance data) | `india_valuation_scores` | Free |
| 12 | Smart Money Fetch | Fetcher | NSE Bulk Deals + BSE Shareholding | `india_bulk_deals`, `india_shareholding_patterns` | Free |
| 13 | Smart Money Scoring | Scorer | `india_bulk_deals` + `india_shareholding_patterns` | `india_smart_money_scores` | Free |
| 14 | Degradation Monitoring | Scorer | All existing tables | `india_degradation_flags` | Free |
| 15 | Composite Gem Scoring | Processing | All score tables | `india_hidden_gems` | ~$0.005/thesis |
| 16 | Output | Display | `india_hidden_gems` | -- | Free |

### Cost Summary Per Full Pipeline Run

| Cost Item | Unit Cost | Volume | Total |
|-----------|-----------|--------|-------|
| Corporate Intelligence (Claude) | ~$0.02/filing | Max 40/run | ~$0.80 |
| Thesis Synthesis (Claude) | ~$0.005/thesis | ~10-20 HIGH+ companies | ~$0.05-0.10 |
| **Total per run** | | | **~$0.85-0.90** |

All other components (14 of 16 steps) are pure Python with zero API cost.

### Database Tables Reference

| Table | Primary Key | Purpose |
|-------|-------------|---------|
| `india_companies` | `isin` | Universe of ~300 mid/small-cap companies |
| `india_promoter_signals` | `id` | Raw insider trading disclosures from BSE |
| `india_promoter_summary` | `isin` | Aggregated promoter scores |
| `india_financials_history` | `isin + period_end + period_type` | Annual and quarterly financials from Screener.in |
| `india_operating_leverage_scores` | `isin` | OL layer scores |
| `india_quality_scores` | `isin` | Quality layer scores |
| `india_policy_scores` | `isin` | Policy layer scores |
| `india_corporate_filings` | `id` | Raw NSE corporate announcements |
| `india_corporate_intelligence_scores` | `isin` | Corporate intelligence layer scores |
| `india_valuation_scores` | `isin` | Valuation modifier scores |
| `india_bulk_deals` | `id` | NSE bulk/block deal records |
| `india_shareholding_patterns` | `isin + quarter` | BSE quarterly shareholding data |
| `india_smart_money_scores` | `isin` | Smart money modifier scores |
| `india_degradation_flags` | `isin` | Degradation modifier scores |
| `india_hidden_gems` | `isin` | Final composite scores and theses |
| `india_concall_signals` | `isin` | Legacy concall signals (deprecated, used as fallback) |

---

*This document reflects the scoring methodology as implemented in the codebase. All score computations, breakpoints, and thresholds are derived directly from the source files listed in each section.*
