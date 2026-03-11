"""
nse_shareholding_fetcher.py
Fetches quarterly shareholding pattern data from NSE's official API.
Replaces BSE shareholding dependency — uses NSE ticker directly, no BSE code needed.

API endpoint:
  GET https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol={SYMBOL}
  No auth required — just session cookies from NSE homepage.
  Returns shareholding breakdown by category.

Data flow:
  NSE API → parse categories → detect superstars → compute deltas → upsert india_shareholding_patterns
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import structlog

log = structlog.get_logger()

NSE_BASE = "https://www.nseindia.com"
SYMBOL_DELAY_SEC = 2.0
MAX_RETRIES = 3

DATA_DIR = Path(__file__).parent.parent / "data"


def _load_superstar_investors() -> list:
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


def _get_current_quarter() -> str:
    """
    Returns current quarter string in Indian FY format.
    Apr-Jun = Q1, Jul-Sep = Q2, Oct-Dec = Q3, Jan-Mar = Q4
    """
    now = datetime.now()
    month = now.month

    if 4 <= month <= 6:
        quarter_num = 1
        fy_year = now.year + 1
    elif 7 <= month <= 9:
        quarter_num = 2
        fy_year = now.year + 1
    elif 10 <= month <= 12:
        quarter_num = 3
        fy_year = now.year + 1
    else:
        quarter_num = 4
        fy_year = now.year

    fy_suffix = str(fy_year % 100).zfill(2)
    return f"Q{quarter_num}FY{fy_suffix}"


def _get_previous_quarter(quarter: str) -> str:
    """Given 'Q3FY26', returns 'Q2FY26'. Handles FY rollover."""
    if len(quarter) < 5 or not quarter.startswith("Q"):
        return quarter

    quarter_num = int(quarter[1])
    fy_suffix = int(quarter[4:])

    if quarter_num == 1:
        return f"Q4FY{str(fy_suffix - 1).zfill(2)}"
    return f"Q{quarter_num - 1}FY{str(fy_suffix).zfill(2)}"


async def _get_nse_session(client: httpx.AsyncClient) -> None:
    """
    Seed NSE session cookies via /option-chain page.
    The homepage alone does NOT set all required cookies — the /option-chain
    page sets the specific cookies NSE's API layer requires.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/118.0",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.nseindia.com/get-quotes/equity?symbol=HDFCBANK",
    }
    try:
        resp = await client.get(
            f"{NSE_BASE}/option-chain",
            headers=headers,
            follow_redirects=True,
        )
        log.info("nse_shareholding_session_established", status=resp.status_code,
                 cookies=len(client.cookies))
    except Exception as exc:
        log.warning("nse_shareholding_session_failed", error=str(exc)[:100])


def _detect_superstars(holders: list, investors: list) -> list:
    """Match holder names against superstar investor list."""
    if not investors or not holders:
        return holders

    # Build alias lookup
    alias_map = {}
    for investor in investors:
        canonical = investor.get("name", "")
        for alias in investor.get("aliases", []):
            alias_map[alias.lower().strip()] = canonical

    enriched = []
    for holder in holders:
        holder_name_lower = (holder.get("name", "") or "").lower().strip()
        matched = None

        # Exact alias match
        if holder_name_lower in alias_map:
            matched = alias_map[holder_name_lower]
        else:
            # Substring match
            for alias, canonical in alias_map.items():
                if alias in holder_name_lower or holder_name_lower in alias:
                    matched = canonical
                    break

        enriched.append({
            **holder,
            "is_superstar": matched is not None,
            "superstar_name": matched,
        })

    return enriched


def _parse_shareholding_response(data) -> Optional[dict]:
    """
    Parse NSE shareholding API response into normalized percentages.

    NSE returns a JSON array of quarterly records (most recent first).
    Each record has: pr_and_prgrp (promoter %), public_val (public %),
    employeeTrusts, date, name, symbol, etc.

    The API provides only promoter vs public split — not the detailed
    FII/DII/MF breakdown. For basic tracking this is sufficient.
    """
    try:
        rows = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in ["data", "shareholding", "shareHolding", "Table"]:
                if key in data and isinstance(data[key], list):
                    rows = data[key]
                    break
            if not rows:
                for val in data.values():
                    if isinstance(val, list) and len(val) > 0:
                        rows = val
                        break

        if not rows:
            return None

        # Take the most recent record (first in the list)
        latest = rows[0]

        # NSE shareholding master returns pr_and_prgrp and public_val
        promoter_pct = 0.0
        public_pct = 0.0

        # Try the known NSE field names first
        for promo_key in ["pr_and_prgrp", "promoterAndPromoterGroup",
                          "promoter_pct", "promoter"]:
            if promo_key in latest and latest[promo_key]:
                try:
                    promoter_pct = float(str(latest[promo_key]).replace("%", "").replace(",", "").strip())
                    break
                except (ValueError, TypeError):
                    continue

        for pub_key in ["public_val", "publicShareholding", "public_pct", "public"]:
            if pub_key in latest and latest[pub_key]:
                try:
                    public_pct = float(str(latest[pub_key]).replace("%", "").replace(",", "").strip())
                    break
                except (ValueError, TypeError):
                    continue

        # Validate — both should be > 0 and sum to ~100
        if promoter_pct < 0.1 and public_pct < 0.1:
            return None

        # Employee trusts
        employee_pct = 0.0
        if "employeeTrusts" in latest and latest["employeeTrusts"]:
            try:
                employee_pct = float(str(latest["employeeTrusts"]).replace("%", "").strip())
            except (ValueError, TypeError):
                pass

        # DII/FII/MF will be 0 from this API — we store what we have
        result = {
            "promoter_pct": round(promoter_pct, 2),
            "fii_pct": 0.0,
            "dii_pct": 0.0,
            "mf_pct": 0.0,
            "insurance_pct": 0.0,
            "public_pct": round(public_pct, 2),
            "notable_holders": [],
            "filing_date": latest.get("date", ""),
        }

        # If the name field is present, add it as a holder reference
        company_name = latest.get("name", "")
        if company_name:
            result["company_name"] = company_name

        return result

    except Exception as exc:
        log.warning("shareholding_parse_error", error=str(exc)[:120])
        return None


async def fetch_shareholding_for_symbol(
    client: httpx.AsyncClient,
    symbol: str,
) -> Optional[dict]:
    """
    Fetch one company's shareholding pattern from NSE API.
    Uses ticker directly — no BSE code mapping needed.
    """
    url = f"{NSE_BASE}/api/corporate-share-holdings-master"
    params = {
        "index": "equities",
        "symbol": symbol,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:109.0) Gecko/20100101 Firefox/118.0",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}",
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(url, params=params, headers=headers, timeout=30)

            if resp.status_code == 403:
                log.warning("nse_shareholding_403_reauth", symbol=symbol, attempt=attempt + 1)
                await _get_nse_session(client)
                await asyncio.sleep(2 ** (attempt + 1))
                continue

            if resp.status_code == 429:
                wait = 2 ** (attempt + 2)
                log.warning("nse_shareholding_rate_limited", symbol=symbol, retry_in=wait)
                await asyncio.sleep(wait)
                continue

            if resp.status_code != 200:
                log.debug("nse_shareholding_api_error", symbol=symbol, status=resp.status_code)
                return None

            data = resp.json()
            parsed = _parse_shareholding_response(data)
            if parsed:
                log.debug("shareholding_fetched", symbol=symbol,
                          promoter_pct=parsed["promoter_pct"])
            return parsed

        except httpx.TimeoutException:
            log.debug("nse_shareholding_timeout", symbol=symbol, attempt=attempt + 1)
            await asyncio.sleep(2 ** attempt)
        except Exception as exc:
            log.debug("nse_shareholding_fetch_error", symbol=symbol, error=str(exc)[:100])
            return None

    return None


def _compute_qoq_deltas(current: dict, previous: Optional[dict]) -> dict:
    """Compute quarter-over-quarter changes in holding percentages."""
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


async def fetch_and_store_shareholding(db, max_companies: int = 2000) -> dict:
    """
    Main entry point. Fetches shareholding patterns for all NSE companies.
    Uses NSE ticker directly — no BSE code needed.

    Steps:
      1. Load superstar investor list
      2. Get all companies from india_companies
      3. For each company, fetch shareholding pattern
      4. Compute QoQ deltas and detect superstar holders
      5. Upsert into india_shareholding_patterns

    Rate limited to 2s between requests.
    """
    results = {
        "fetched": 0,
        "stored": 0,
        "errors": 0,
        "skipped": 0,
        "already_have": 0,
        "companies_processed": 0,
    }

    superstar_investors = _load_superstar_investors()
    current_quarter = _get_current_quarter()
    previous_quarter = _get_previous_quarter(current_quarter)

    # Get all companies
    from india_alpha.db import fetch_all_rows
    companies = await fetch_all_rows(
        db, "india_companies",
        select="isin, ticker",
    )

    if not companies:
        log.warning("nse_shareholding_no_companies")
        return results

    # Sort by ticker for predictable ordering
    companies.sort(key=lambda c: c.get("ticker", ""))
    companies = companies[:max_companies]

    # Resume support: check which companies already have data for current quarter
    try:
        existing_result = await fetch_all_rows(
            db, "india_shareholding_patterns",
            select="isin",
            eq={"quarter": current_quarter},
        )
        existing_isins = {r["isin"] for r in existing_result}
    except Exception:
        existing_isins = set()

    log.info("nse_shareholding_fetch_start",
             companies=len(companies),
             already_have=len(existing_isins),
             quarter=current_quarter)

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        await _get_nse_session(client)
        await asyncio.sleep(1)

        consecutive_failures = 0

        for idx, company in enumerate(companies):
            isin = company.get("isin", "")
            ticker = company.get("ticker", "")

            if not isin or not ticker:
                results["skipped"] += 1
                continue

            # Skip if already fetched this quarter
            if isin in existing_isins:
                results["already_have"] += 1
                continue

            try:
                current_data = await fetch_shareholding_for_symbol(client, ticker)
                results["companies_processed"] += 1

                if current_data is None:
                    results["skipped"] += 1
                    consecutive_failures += 1

                    # If 10 consecutive failures, re-establish session
                    if consecutive_failures >= 10:
                        log.warning("nse_shareholding_many_failures", count=consecutive_failures)
                        await _get_nse_session(client)
                        await asyncio.sleep(3)
                        consecutive_failures = 0

                    await asyncio.sleep(SYMBOL_DELAY_SEC)
                    continue

                consecutive_failures = 0
                results["fetched"] += 1

                # Check for previous quarter data in DB for QoQ deltas
                previous_data = None
                try:
                    prev_result = await db.table("india_shareholding_patterns") \
                        .select("*") \
                        .eq("isin", isin) \
                        .eq("quarter", previous_quarter) \
                        .limit(1) \
                        .execute()
                    if prev_result.data:
                        previous_data = prev_result.data[0]
                except Exception:
                    pass

                deltas = _compute_qoq_deltas(current_data, previous_data)

                # Detect superstar investors
                enriched_holders = _detect_superstars(
                    current_data.get("notable_holders", []),
                    superstar_investors,
                )

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

                await db.table("india_shareholding_patterns") \
                    .upsert(record, on_conflict="isin,quarter") \
                    .execute()

                results["stored"] += 1
                log.debug("shareholding_stored", ticker=ticker,
                          promoter=current_data["promoter_pct"],
                          fii=current_data["fii_pct"])

            except Exception as exc:
                results["errors"] += 1
                log.error("shareholding_store_failed",
                          ticker=ticker, error=str(exc)[:120])

            # Progress logging every 50 companies
            if (idx + 1) % 50 == 0:
                log.info("nse_shareholding_progress",
                         processed=idx + 1,
                         total=len(companies),
                         stored=results["stored"])

            # Re-establish session every 100 requests
            if (idx + 1) % 100 == 0:
                await _get_nse_session(client)
                await asyncio.sleep(1)

            await asyncio.sleep(SYMBOL_DELAY_SEC)

    log.info("nse_shareholding_fetch_complete", **results)
    return results
