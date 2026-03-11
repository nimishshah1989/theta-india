"""
nse_insider_fetcher.py
Fetches SEBI PIT (Prohibition of Insider Trading) disclosures from NSE.
Replaces BSE insider dependency — uses NSE's official API since our entire
universe is NSE-listed.

API endpoint:
  GET https://www.nseindia.com/api/corporates-pit?symbol={SYMBOL}&issuer=
  Returns JSON with insider trading disclosures for the given symbol.

Data flow:
  NSE API → parse → classify signal type → upsert india_promoter_signals
"""

import asyncio
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
import structlog

log = structlog.get_logger()

NSE_BASE = "https://www.nseindia.com"
SYMBOL_DELAY_SEC = 2.0
MAX_RETRIES = 3

# Reuse signal classification from bse_insider.py
SIGNAL_TYPE_MAP = {
    "market purchase": "open_market_buy",
    "market sale": "open_market_sell",
    "off market": "off_market",
    "allotment": "preferential_allotment",
    "esos": "esop_exercise",
    "esop": "esop_exercise",
    "sweat equity": "sweat_equity",
    "pledge": "pledge_increase",
    "revocation": "pledge_decrease",
    "invocation": "pledge_increase",
    "transmission": "transmission",
    "gift": "gift",
    "creeping acquisition": "creeping_acquisition",
    "warrant": "warrant_allotment",
    "acquisition": "open_market_buy",
    "disposal": "open_market_sell",
    "buy": "open_market_buy",
    "sell": "open_market_sell",
    "sale": "open_market_sell",
    "purchase": "open_market_buy",
}

SIGNAL_STRENGTH = {
    "open_market_buy":        9,
    "warrant_allotment":      7,
    "creeping_acquisition":   7,
    "pledge_decrease":        6,
    "esop_exercise":          3,
    "preferential_allotment": 3,
    "off_market":             2,
    "sweat_equity":           1,
    "transmission":           0,
    "gift":                   0,
    "open_market_sell":      -5,
    "pledge_increase":       -8,
}


def _classify_signal_type(transaction_type: str, acq_mode: str) -> str:
    """Classify the insider transaction into a signal type."""
    combined = f"{transaction_type} {acq_mode}".lower()

    # Sort by keyword length descending for best match
    for keyword in sorted(SIGNAL_TYPE_MAP.keys(), key=len, reverse=True):
        if keyword in combined:
            return SIGNAL_TYPE_MAP[keyword]
    return "other"


def _parse_nse_date(date_str: str) -> Optional[str]:
    """Parse NSE date format to YYYY-MM-DD string."""
    if not date_str:
        return None

    for fmt in ("%d-%b-%Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


async def _get_nse_session(client: httpx.AsyncClient) -> None:
    """Seed NSE session cookies via /option-chain page (homepage alone doesn't set all required cookies)."""
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
        log.info("nse_insider_session_established", status=resp.status_code,
                 cookies=len(client.cookies))
    except Exception as exc:
        log.warning("nse_insider_session_failed", error=str(exc)[:100])


async def fetch_insider_for_symbol(
    client: httpx.AsyncClient,
    symbol: str,
) -> list:
    """
    Fetch insider trading disclosures for one NSE symbol.
    Returns list of raw dicts from NSE API.
    """
    # NSE insider trading API endpoint
    url = f"{NSE_BASE}/api/corporates-pit"
    params = {
        "symbol": symbol,
        "issuer": "",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{NSE_BASE}/companies-listing/corporate-filings-insider-trading",
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(url, params=params, headers=headers, timeout=30)

            if resp.status_code == 403:
                log.warning("nse_insider_403_reauth", symbol=symbol, attempt=attempt + 1)
                await _get_nse_session(client)
                await asyncio.sleep(2 ** (attempt + 1))
                continue

            if resp.status_code == 429:
                wait = 2 ** (attempt + 2)
                log.warning("nse_insider_rate_limited", symbol=symbol, retry_in=wait)
                await asyncio.sleep(wait)
                continue

            if resp.status_code != 200:
                log.debug("nse_insider_api_error", symbol=symbol, status=resp.status_code)
                return []

            data = resp.json()
            # NSE returns either a list directly or {"data": [...]}
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get("data", [])
            return []

        except httpx.TimeoutException:
            log.debug("nse_insider_timeout", symbol=symbol, attempt=attempt + 1)
            await asyncio.sleep(2 ** attempt)
        except Exception as exc:
            log.debug("nse_insider_fetch_error", symbol=symbol, error=str(exc)[:100])
            return []

    return []


def _parse_nse_insider_record(raw: dict, ticker: str, isin: str) -> Optional[dict]:
    """Parse one NSE insider trading record into our schema."""
    try:
        # NSE field names (may vary)
        transaction_type = (
            raw.get("acqMode", "")
            or raw.get("acquisitionMode", "")
            or raw.get("typeOfTransaction", "")
            or ""
        )
        acq_mode = (
            raw.get("secAcq", "")
            or raw.get("securitiesTypePost", "")
            or raw.get("tdpTransactionType", "")
            or ""
        )

        signal_type = _classify_signal_type(transaction_type, acq_mode)

        # Parse value — NSE provides in various field names
        value_raw = (
            raw.get("secVal", 0)
            or raw.get("securitiesValue", 0)
            or raw.get("valueOfSecurity", 0)
            or 0
        )
        try:
            value_rs = float(str(value_raw).replace(",", "").strip() or "0")
            value_cr = value_rs / 10_000_000  # Convert rupees to crores
        except (ValueError, TypeError):
            value_cr = 0.0

        # Parse shares
        shares_raw = (
            raw.get("secAcq", 0)
            or raw.get("securitiesAcquired", 0)
            or raw.get("noOfSecurities", 0)
            or 0
        )
        try:
            # secAcq can be text (mode) or number depending on the field
            shares_str = str(shares_raw).replace(",", "").strip()
            shares = int(float(shares_str)) if shares_str and shares_str.replace(".", "").isdigit() else 0
        except (ValueError, TypeError):
            shares = 0

        # Parse person details
        person_name = (
            raw.get("acqName", "")
            or raw.get("acquirerName", "")
            or raw.get("nameOfInsider", "")
            or ""
        ).strip()

        person_category = (
            raw.get("personCategory", "")
            or raw.get("categoryOfPerson", "")
            or raw.get("category", "promoter")
            or "promoter"
        ).lower().strip()

        # Parse dates
        txn_date_str = (
            raw.get("date", "")
            or raw.get("acquisitionFromDate", "")
            or raw.get("acqfromDt", "")
            or ""
        )
        intimation_date_str = (
            raw.get("intimDt", "")
            or raw.get("intimationDate", "")
            or raw.get("intimationDt", "")
            or ""
        )

        transaction_date = _parse_nse_date(txn_date_str) or str(date.today())
        intimation_date = _parse_nse_date(intimation_date_str) or str(date.today())

        # Post-transaction percentage
        post_pct_raw = (
            raw.get("befAcqSharesPercentage", 0)
            or raw.get("afterAcqSharesPercentage", 0)
            or raw.get("postShareholding", 0)
            or 0
        )
        try:
            post_pct = float(str(post_pct_raw).replace("%", "").replace(",", "").strip() or "0")
        except (ValueError, TypeError):
            post_pct = 0.0

        company_name = (
            raw.get("company", "")
            or raw.get("symbol", "")
            or ticker
        )

        return {
            "isin": isin,
            "ticker": ticker,
            "company_name": company_name,
            "signal_type": signal_type,
            "transaction_date": transaction_date,
            "intimation_date": intimation_date,
            "person_name": person_name,
            "person_category": person_category,
            "transaction_type": (transaction_type or "").lower()[:100],
            "shares": shares,
            "value_cr": round(value_cr, 4),
            "post_transaction_pct": post_pct,
            "signal_strength": SIGNAL_STRENGTH.get(signal_type, 0),
            "raw_data": raw,
        }
    except Exception as exc:
        log.debug("parse_nse_insider_failed", error=str(exc)[:80])
        return None


async def _is_duplicate(db, record: dict) -> bool:
    """Check if this disclosure is already stored."""
    if not record.get("isin") or not record.get("transaction_date"):
        return False
    try:
        existing = await db.table("india_promoter_signals") \
            .select("id") \
            .eq("isin", record["isin"]) \
            .eq("transaction_date", record["transaction_date"]) \
            .eq("person_name", record["person_name"]) \
            .eq("shares", record["shares"]) \
            .limit(1) \
            .execute()
        return len(existing.data or []) > 0
    except Exception:
        return False


async def fetch_and_store_insider_signals(db, days_back: int = 365) -> dict:
    """
    Main entry point. Fetches NSE insider trading disclosures for all
    companies in the universe and stores new ones.

    Iterates over all symbols in india_companies, fetching PIT disclosures
    from NSE's API. Rate-limited to 2s between requests.

    Args:
        db: Async Supabase client
        days_back: How far back to consider transactions (default 365 days)

    Returns:
        Summary dict with fetched/new/skipped/errors/companies_processed counts
    """
    results = {
        "fetched": 0,
        "new": 0,
        "skipped": 0,
        "errors": 0,
        "companies_processed": 0,
        "companies_with_data": 0,
    }

    # Get all companies from universe
    from india_alpha.db import fetch_all_rows
    companies = await fetch_all_rows(
        db, "india_companies",
        select="ticker, isin",
    )

    if not companies:
        log.warning("nse_insider_no_companies")
        return results

    # Sort by ticker for predictable ordering
    companies.sort(key=lambda c: c.get("ticker", ""))
    log.info("nse_insider_fetch_start", companies=len(companies))

    cutoff_date = date.today() - timedelta(days=days_back)

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        # Establish NSE session
        await _get_nse_session(client)
        await asyncio.sleep(1)

        consecutive_empty = 0

        for idx, company in enumerate(companies):
            ticker = company.get("ticker", "")
            isin = company.get("isin", "")

            if not ticker or not isin:
                results["skipped"] += 1
                continue

            try:
                raw_records = await fetch_insider_for_symbol(client, ticker)
                results["companies_processed"] += 1

                if raw_records:
                    consecutive_empty = 0
                    results["companies_with_data"] += 1
                else:
                    consecutive_empty += 1

                for raw in raw_records:
                    results["fetched"] += 1
                    record = _parse_nse_insider_record(raw, ticker, isin)

                    if not record or not record.get("person_name"):
                        results["skipped"] += 1
                        continue

                    # Filter old transactions
                    try:
                        txn_date = date.fromisoformat(record["transaction_date"])
                        if txn_date < cutoff_date:
                            results["skipped"] += 1
                            continue
                    except (ValueError, TypeError):
                        pass

                    # Skip duplicates
                    if await _is_duplicate(db, record):
                        results["skipped"] += 1
                        continue

                    try:
                        await db.table("india_promoter_signals").insert(record).execute()
                        results["new"] += 1
                    except Exception as exc:
                        log.debug("nse_insider_store_failed",
                                  ticker=ticker, error=str(exc)[:100])
                        results["errors"] += 1

            except Exception as exc:
                log.error("nse_insider_company_failed",
                          ticker=ticker, error=str(exc)[:120])
                results["errors"] += 1

            # Progress logging every 50 companies
            if (idx + 1) % 50 == 0:
                log.info("nse_insider_progress",
                         processed=idx + 1,
                         total=len(companies),
                         new_signals=results["new"])

            # Re-establish session every 100 requests
            if (idx + 1) % 100 == 0:
                await _get_nse_session(client)
                await asyncio.sleep(1)

            # Rate limit
            await asyncio.sleep(SYMBOL_DELAY_SEC)

    log.info("nse_insider_fetch_complete", **results)
    return results
