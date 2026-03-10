"""
bse_shareholding_fetcher.py
Fetches quarterly shareholding pattern data from BSE's public API.
Tracks promoter, FII, DII, MF, and insurance holding percentages per quarter.
Detects superstar investor presence using fuzzy name matching against known list.

Data flow:
  BSE ShareHolding API → parse categories → detect superstars → upsert india_shareholding_patterns

BSE API endpoint:
  GET https://api.bseindia.com/BseIndiaAPI/api/CorporateAction/GetShareHoldingPattern
  Params: scripcode (BSE code), qtrid (quarter ID like "106.00")

Note: BSE API can be unreliable — this module is fully defensive.
If the API is unavailable, it returns gracefully with zero records.
"""

import json
import asyncio
import httpx
import structlog
from datetime import datetime
from pathlib import Path
from typing import Optional

log = structlog.get_logger()

# BSE endpoints
BSE_HOMEPAGE = "https://www.bseindia.com/"
BSE_SHAREHOLDING_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/CorporateAction/GetShareHoldingPattern"
)

# Standard headers to mimic browser session
BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}

# Superstar investor data lives alongside policy_registry.json
DATA_DIR = Path(__file__).parent.parent / "data"

# BSE quarter IDs — they use a numeric scheme where recent quarters have
# higher IDs. We try a range of recent IDs to find matching data.
# These approximate recent quarter IDs as of FY26.
RECENT_QUARTER_IDS = [
    "120.00", "119.00", "118.00", "117.00",
    "116.00", "115.00", "114.00", "113.00",
    "112.00", "111.00", "110.00", "109.00",
    "108.00", "107.00", "106.00", "105.00",
]


def _load_superstar_investors() -> list[dict]:
    """Load superstar investor list from JSON data file."""
    superstar_path = DATA_DIR / "superstar_investors.json"
    try:
        with open(superstar_path, "r") as f:
            data = json.load(f)
        investors = data.get("investors", [])
        log.info("superstar_investors_loaded", count=len(investors))
        return investors
    except FileNotFoundError:
        log.warning("superstar_investors_file_missing", path=str(superstar_path))
        return []
    except json.JSONDecodeError as exc:
        log.error("superstar_investors_json_invalid", error=str(exc)[:100])
        return []


async def _get_bse_session(client: httpx.AsyncClient) -> None:
    """
    Hit BSE homepage to establish session cookies.
    The client object retains cookies for subsequent API calls.
    """
    try:
        await client.get(BSE_HOMEPAGE, headers={
            "User-Agent": BSE_HEADERS["User-Agent"],
            "Accept": "text/html,application/xhtml+xml",
        })
        log.debug("bse_session_established")
    except Exception as exc:
        log.warning("bse_session_failed", error=str(exc)[:100])


def _get_current_quarter() -> str:
    """
    Returns current quarter string in Indian FY format.
    Indian financial year: April-March.
      Apr-Jun = Q1, Jul-Sep = Q2, Oct-Dec = Q3, Jan-Mar = Q4
    Example: January 2026 -> Q4FY26, October 2025 -> Q3FY26
    """
    now = datetime.now()
    month = now.month
    year = now.year

    if month >= 4 and month <= 6:
        quarter_num = 1
        fy_year = year + 1
    elif month >= 7 and month <= 9:
        quarter_num = 2
        fy_year = year + 1
    elif month >= 10 and month <= 12:
        quarter_num = 3
        fy_year = year + 1
    else:
        # Jan-Mar
        quarter_num = 4
        fy_year = year

    # FY year is represented as last 2 digits (FY26 = 2025-26)
    fy_suffix = str(fy_year % 100).zfill(2)
    return f"Q{quarter_num}FY{fy_suffix}"


def _get_previous_quarter(quarter: str) -> str:
    """
    Given "Q3FY26", returns "Q2FY26".
    Handles FY rollover: Q1FY26 -> Q4FY25.
    """
    if len(quarter) < 5 or not quarter.startswith("Q"):
        log.warning("invalid_quarter_format", quarter=quarter)
        return quarter

    quarter_num = int(quarter[1])
    fy_suffix = int(quarter[4:])

    if quarter_num == 1:
        # Roll back to Q4 of previous FY
        prev_quarter_num = 4
        prev_fy = fy_suffix - 1
    else:
        prev_quarter_num = quarter_num - 1
        prev_fy = fy_suffix

    return f"Q{prev_quarter_num}FY{str(prev_fy).zfill(2)}"


def _detect_superstars(
    notable_holders: list[dict],
    investors: list[dict],
) -> list[dict]:
    """
    Match holder names against superstar investor list using alias matching.
    Enriches each holder dict with is_superstar and superstar_name fields.
    """
    if not investors:
        return notable_holders

    # Build a flat lookup: lowercase alias -> superstar canonical name
    alias_map: dict[str, str] = {}
    for investor in investors:
        canonical_name = investor.get("name", "")
        for alias in investor.get("aliases", []):
            alias_map[alias.lower().strip()] = canonical_name

    enriched = []
    for holder in notable_holders:
        holder_name_lower = (holder.get("name", "") or "").lower().strip()
        matched_superstar: Optional[str] = None

        # Exact alias match first
        if holder_name_lower in alias_map:
            matched_superstar = alias_map[holder_name_lower]
        else:
            # Substring match — check if any alias appears within the holder name
            for alias, canonical in alias_map.items():
                if alias in holder_name_lower or holder_name_lower in alias:
                    matched_superstar = canonical
                    break

        enriched.append({
            **holder,
            "is_superstar": matched_superstar is not None,
            "superstar_name": matched_superstar,
        })

    return enriched


def _parse_shareholding_response(data) -> Optional[dict]:
    """
    Parse BSE shareholding API response into normalized category percentages.
    BSE returns data in various formats — we handle the common structures.

    Expected BSE response contains tables with category-wise shareholding.
    Categories typically include:
      - Promoter & Promoter Group
      - Foreign Institutional Investors / FPI
      - Domestic Institutional Investors (MFs, Insurance, Banks)
      - Public / Non-Institutional
    """
    try:
        # BSE can return as a list of category rows or nested table structure
        rows = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            # Try common keys BSE uses
            for key in ["Table", "Table1", "data", "shareHoldingPattern"]:
                if key in data and isinstance(data[key], list):
                    rows = data[key]
                    break

        if not rows:
            return None

        result = {
            "promoter_pct": 0.0,
            "fii_pct": 0.0,
            "dii_pct": 0.0,
            "mf_pct": 0.0,
            "insurance_pct": 0.0,
            "public_pct": 0.0,
            "notable_holders": [],
        }

        for row in rows:
            # BSE uses various field names depending on API version
            category = (
                row.get("CATEGORY", "")
                or row.get("category", "")
                or row.get("Category", "")
                or row.get("shareholderCategory", "")
                or ""
            ).lower()

            pct_str = (
                row.get("PERCENTAGE", "0")
                or row.get("percentage", "0")
                or row.get("Percentage", "0")
                or row.get("shareholdingPercentage", "0")
                or "0"
            )

            try:
                pct = float(str(pct_str).replace("%", "").strip())
            except (ValueError, TypeError):
                pct = 0.0

            # Classify into our buckets based on category keywords
            if any(kw in category for kw in ["promoter", "promotor"]):
                result["promoter_pct"] += pct
            elif any(kw in category for kw in ["foreign", "fii", "fpi"]):
                result["fii_pct"] += pct
            elif "mutual fund" in category or "mutual_fund" in category:
                result["mf_pct"] += pct
                result["dii_pct"] += pct
            elif "insurance" in category:
                result["insurance_pct"] += pct
                result["dii_pct"] += pct
            elif any(kw in category for kw in [
                "bank", "financial institution", "pension", "provident",
                "alternate investment", "aif",
            ]):
                result["dii_pct"] += pct
            elif any(kw in category for kw in ["public", "non-institution", "individual"]):
                result["public_pct"] += pct

            # Collect notable individual holders (those with > 1% holding)
            holder_name = (
                row.get("NAME", "")
                or row.get("name", "")
                or row.get("Name", "")
                or row.get("shareholderName", "")
                or ""
            ).strip()

            if holder_name and pct >= 1.0:
                result["notable_holders"].append({
                    "name": holder_name,
                    "pct": round(pct, 2),
                })

        # Validate — if all zeros, the parse probably failed
        total = result["promoter_pct"] + result["fii_pct"] + result["public_pct"]
        if total < 1.0:
            log.debug("shareholding_parse_yielded_zeros", total=total)
            return None

        # Round everything
        for key in ["promoter_pct", "fii_pct", "dii_pct", "mf_pct", "insurance_pct", "public_pct"]:
            result[key] = round(result[key], 2)

        return result

    except Exception as exc:
        log.warning("shareholding_parse_error", error=str(exc)[:120])
        return None


async def fetch_shareholding_for_company(
    client: httpx.AsyncClient,
    scrip_code: str,
    quarter: str,
) -> Optional[dict]:
    """
    Fetch one company's shareholding pattern from BSE API.
    Tries multiple quarter IDs since BSE uses numeric IDs that don't
    map directly to our Q1FY26 format.
    Returns parsed dict or None if API fails.
    """
    if not scrip_code:
        return None

    # Try the two most recent quarter IDs (BSE numeric format)
    for quarter_id in RECENT_QUARTER_IDS[:4]:
        try:
            params = {
                "scripcode": str(scrip_code),
                "qtrid": quarter_id,
            }
            resp = await client.get(
                BSE_SHAREHOLDING_URL,
                params=params,
                headers=BSE_HEADERS,
            )

            if resp.status_code != 200:
                continue

            raw_data = resp.json()
            parsed = _parse_shareholding_response(raw_data)
            if parsed is not None:
                log.debug(
                    "shareholding_fetched",
                    scrip_code=scrip_code,
                    quarter_id=quarter_id,
                    promoter_pct=parsed["promoter_pct"],
                )
                return parsed

        except httpx.TimeoutException:
            log.debug("shareholding_timeout", scrip_code=scrip_code, quarter_id=quarter_id)
            continue
        except Exception as exc:
            log.debug("shareholding_fetch_error", scrip_code=scrip_code, error=str(exc)[:80])
            continue

    return None


def _compute_qoq_deltas(current: dict, previous: Optional[dict]) -> dict:
    """
    Compute quarter-over-quarter changes in holding percentages.
    If no previous data available, all deltas default to 0.
    """
    if previous is None:
        return {
            "promoter_delta": 0.0,
            "fii_delta": 0.0,
            "mf_delta": 0.0,
            "dii_delta": 0.0,
        }

    return {
        "promoter_delta": round(current.get("promoter_pct", 0) - previous.get("promoter_pct", 0), 2),
        "fii_delta": round(current.get("fii_pct", 0) - previous.get("fii_pct", 0), 2),
        "mf_delta": round(current.get("mf_pct", 0) - previous.get("mf_pct", 0), 2),
        "dii_delta": round(current.get("dii_pct", 0) - previous.get("dii_pct", 0), 2),
    }


async def fetch_and_store_shareholding(db, max_companies: int = 200) -> dict:
    """
    Main entry point — fetches shareholding patterns for all companies with BSE codes.

    Steps:
      1. Load superstar investor list
      2. Get companies from india_companies that have a bse_code
      3. For each company, fetch current + previous quarter shareholding
      4. Compute QoQ deltas and detect superstar holders
      5. Upsert into india_shareholding_patterns (on conflict: isin + quarter)

    Rate limited to 1 request per 2 seconds to respect BSE.
    """
    results = {"fetched": 0, "stored": 0, "errors": 0, "skipped": 0}

    # Load superstar investors for name matching
    superstar_investors = _load_superstar_investors()

    # Get companies with BSE codes
    try:
        company_resp = await db.table("india_companies") \
            .select("isin, ticker, bse_code") \
            .neq("bse_code", "") \
            .not_.is_("bse_code", "null") \
            .limit(max_companies) \
            .execute()
        companies = company_resp.data or []
    except Exception as exc:
        log.error("shareholding_company_fetch_failed", error=str(exc)[:120])
        return {**results, "skipped_reason": "company_list_unavailable"}

    if not companies:
        log.warning("no_companies_with_bse_code")
        return {**results, "skipped_reason": "no_bse_codes"}

    log.info("shareholding_fetch_starting", companies=len(companies))

    current_quarter = _get_current_quarter()
    previous_quarter = _get_previous_quarter(current_quarter)

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        # Establish BSE session cookies
        await _get_bse_session(client)

        # Quick check: test one company to see if API is responsive
        test_company = companies[0]
        test_result = await fetch_shareholding_for_company(
            client, test_company.get("bse_code", ""), current_quarter,
        )
        if test_result is None and len(companies) > 1:
            # Try one more before giving up
            test_result = await fetch_shareholding_for_company(
                client, companies[1].get("bse_code", ""), current_quarter,
            )
            if test_result is None:
                log.warning("bse_shareholding_api_unavailable")
                return {**results, "skipped_reason": "api_unavailable"}

        for company in companies:
            isin = company.get("isin", "")
            ticker = company.get("ticker", "")
            bse_code = company.get("bse_code", "")

            if not bse_code or not isin:
                results["skipped"] += 1
                continue

            try:
                # Fetch current quarter
                current_data = await fetch_shareholding_for_company(
                    client, bse_code, current_quarter,
                )

                if current_data is None:
                    results["skipped"] += 1
                    log.debug("shareholding_no_data", ticker=ticker, bse_code=bse_code)
                    await asyncio.sleep(2)
                    continue

                results["fetched"] += 1

                # Fetch previous quarter for QoQ deltas
                previous_data = await fetch_shareholding_for_company(
                    client, bse_code, previous_quarter,
                )

                # Compute changes
                deltas = _compute_qoq_deltas(current_data, previous_data)

                # Detect superstar investors in notable holders
                enriched_holders = _detect_superstars(
                    current_data.get("notable_holders", []),
                    superstar_investors,
                )

                # Build record for upsert
                record = {
                    "isin": isin,
                    "ticker": ticker,
                    "quarter": current_quarter,
                    "promoter_pct": current_data["promoter_pct"],
                    "fii_pct": current_data["fii_pct"],
                    "dii_pct": current_data["dii_pct"],
                    "mf_pct": current_data["mf_pct"],
                    "insurance_pct": current_data["insurance_pct"],
                    "public_pct": current_data["public_pct"],
                    "notable_holders": json.dumps(enriched_holders),
                    "promoter_delta": deltas["promoter_delta"],
                    "fii_delta": deltas["fii_delta"],
                    "mf_delta": deltas["mf_delta"],
                    "dii_delta": deltas["dii_delta"],
                    "fetched_at": datetime.now().isoformat(),
                }

                # Upsert — update if same company+quarter already exists
                await db.table("india_shareholding_patterns") \
                    .upsert(record, on_conflict="isin,quarter") \
                    .execute()

                results["stored"] += 1
                log.debug(
                    "shareholding_stored",
                    ticker=ticker,
                    promoter=current_data["promoter_pct"],
                    fii=current_data["fii_pct"],
                    superstars=[h["superstar_name"] for h in enriched_holders if h.get("is_superstar")],
                )

            except Exception as exc:
                results["errors"] += 1
                log.error(
                    "shareholding_store_failed",
                    ticker=ticker,
                    error=str(exc)[:120],
                )

            # Rate limit — respect BSE servers
            await asyncio.sleep(2)

    log.info("shareholding_fetch_complete", **results)
    return results
