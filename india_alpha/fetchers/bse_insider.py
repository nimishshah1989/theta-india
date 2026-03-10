"""
bse_insider.py
Fetches SEBI PIT (Prohibition of Insider Trading) disclosures from BSE.
Promoter open-market buying is the strongest single signal in Indian markets.
Person who knows the business best is paying market price with personal capital.

Data flow:
  BSE API (primary) → insiderscreener.com (fallback) → stored in india_promoter_signals
"""

import httpx
import asyncio
import structlog
from datetime import date, timedelta, datetime
from typing import Optional

log = structlog.get_logger()

# BSE Insider Trading endpoints
BSE_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/InsiderTrading/w"
BSE_HOMEPAGE  = "https://www.bseindia.com/"

# InsiderScreener — reliable aggregator of BSE/NSE PIT disclosures
INSIDER_SCREENER_URL = "https://www.insiderscreener.com/en/api/india/latest/"


SIGNAL_TYPE_MAP = {
    "market purchase": "open_market_buy",
    "market sale": "open_market_sell",
    "off market": "off_market",
    "allotment": "preferential_allotment",
    "esos": "esop_exercise",
    "sweat equity": "sweat_equity",
    "pledge": "pledge_increase",
    "revocation": "pledge_decrease",
    "transmission": "transmission",
    "gift": "gift",
    "creeping acquisition": "creeping_acquisition",
    "warrant": "warrant_allotment",
}

SIGNAL_STRENGTH = {
    "open_market_buy":       9,   # Strongest — personal capital at market price
    "warrant_allotment":     7,   # Premium price = private valuation signal
    "creeping_acquisition":  7,   # Systematic accumulation near SEBI ceiling
    "pledge_decrease":       6,   # Financial health improving
    "esop_exercise":         3,   # Expected, less informative
    "preferential_allotment":3,   # Below market sometimes
    "off_market":            2,   # Could be family transfer
    "open_market_sell":     -5,   # Negative signal
    "pledge_increase":      -8,   # Stress signal
}


def classify_signal_type(transaction: str, mode: str) -> str:
    t = (transaction or "").lower()
    m = (mode or "").lower()
    combined = f"{t} {m}"

    for keyword, signal_type in SIGNAL_TYPE_MAP.items():
        if keyword in combined:
            return signal_type
    return "other"


async def get_bse_session_cookies() -> dict:
    """BSE requires a valid session cookie for API calls."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        try:
            await client.get(BSE_HOMEPAGE)
            return dict(client.cookies)
        except:
            return {}


async def fetch_from_bse_api(from_date: date, to_date: date) -> list[dict]:
    """Primary source: BSE's own API."""
    cookies = await get_bse_session_cookies()
    params = {
        "fromdate": from_date.strftime("%Y%m%d"),
        "todate": to_date.strftime("%Y%m%d"),
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.bseindia.com/markets/equity/EQReports/InsiderTrading.aspx",
        "Origin": "https://www.bseindia.com",
    }
    async with httpx.AsyncClient(timeout=20, headers=headers, cookies=cookies) as client:
        try:
            resp = await client.get(BSE_API_URL, params=params)
            if resp.status_code == 200:
                data = resp.json()
                records = data.get("Table", data.get("Table1", []))
                log.info("bse_api_success", count=len(records))
                return records
        except Exception as e:
            log.warning("bse_api_failed", error=str(e)[:100])
    return []


async def fetch_from_insiderscreener() -> list[dict]:
    """Fallback: insiderscreener.com aggregates BSE+NSE PIT disclosures."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://www.insiderscreener.com/",
    }
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        try:
            resp = await client.get(INSIDER_SCREENER_URL)
            if resp.status_code == 200:
                data = resp.json()
                records = data.get("data", data if isinstance(data, list) else [])
                log.info("insiderscreener_success", count=len(records))
                return records
        except Exception as e:
            log.warning("insiderscreener_failed", error=str(e)[:100])
    return []


def parse_bse_record(raw: dict) -> Optional[dict]:
    """Parse BSE API record format into our schema."""
    try:
        transaction = raw.get("Type_of_transaction", raw.get("typeoftransaction", ""))
        mode = raw.get("Mode_of_acquisition", raw.get("modeofacq", ""))
        signal_type = classify_signal_type(transaction, mode)

        # Parse value
        try:
            # BSE API returns value in Rupees — divide by 1 Crore (10^7)
            value_cr = float(raw.get("Value_of_Securities",
                             raw.get("valueofsecurities", 0)) or 0) / 10_000_000
        except (ValueError, TypeError):
            value_cr = 0.0

        # Parse shares
        try:
            shares = int(float(raw.get("No_of_securities_acquired_disposed",
                               raw.get("noofsecacquired", 0)) or 0))
        except (ValueError, TypeError):
            shares = 0

        return {
            "isin": raw.get("ISIN", raw.get("isin", "")) or "",
            "ticker": raw.get("NSE_CD", raw.get("nse_cd",
                     raw.get("BSE_CD", raw.get("bse_cd", "")))) or "",
            "company_name": raw.get("Company_Name", raw.get("companyname", "")) or "",
            "signal_type": signal_type,
            "transaction_date": (
                raw.get("Date_of_Allotment_acquisition_disposal",
                raw.get("acqdispdate", str(date.today()))) or str(date.today())
            )[:10],
            "intimation_date": (
                raw.get("Date_of_Intimation_to_company",
                raw.get("intimationdate", str(date.today()))) or str(date.today())
            )[:10],
            "person_name": raw.get("Name_of_the_Insider",
                          raw.get("nameofinsider", "")) or "",
            "person_category": (raw.get("Category_of_Person",
                                raw.get("categoryofperson", "promoter")) or "promoter").lower(),
            "transaction_type": (transaction or "").lower()[:100],
            "shares": shares,
            "value_cr": round(value_cr, 4),
            "post_transaction_pct": float(
                raw.get("Post_transaction_Shareholding_percentage",
                raw.get("posttranspct", 0)) or 0
            ),
            "signal_strength": SIGNAL_STRENGTH.get(signal_type, 0),
            "raw_data": raw,
        }
    except Exception as e:
        log.debug("parse_bse_record_failed", error=str(e)[:80])
        return None


def parse_insiderscreener_record(raw: dict) -> Optional[dict]:
    """Parse insiderscreener.com record format."""
    try:
        transaction = raw.get("transaction_type", "")
        mode = raw.get("mode", "")
        signal_type = classify_signal_type(transaction, mode)

        return {
            "isin": raw.get("isin", "") or "",
            "ticker": raw.get("nse_symbol", raw.get("symbol", "")) or "",
            "company_name": raw.get("company", "") or "",
            "signal_type": signal_type,
            "transaction_date": str(raw.get("date", date.today()))[:10],
            "intimation_date": str(raw.get("filing_date", date.today()))[:10],
            "person_name": raw.get("name", "") or "",
            "person_category": (raw.get("category", "promoter") or "promoter").lower(),
            "transaction_type": (transaction or "").lower()[:100],
            "shares": int(raw.get("shares", 0) or 0),
            "value_cr": float(raw.get("value_cr", 0) or 0),
            "post_transaction_pct": float(raw.get("post_holding", 0) or 0),
            "signal_strength": SIGNAL_STRENGTH.get(signal_type, 0),
            "raw_data": raw,
        }
    except Exception as e:
        log.debug("parse_insiderscreener_failed", error=str(e)[:80])
        return None


async def is_duplicate(db, record: dict) -> bool:
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
            .execute()
        return len(existing.data) > 0
    except:
        return False


async def fetch_and_store_insider_signals(db, days_back: int = 7) -> dict:
    """
    Main entry point.
    Fetches BSE insider trading disclosures and stores new ones.
    """
    results = {"fetched": 0, "new": 0, "skipped": 0, "errors": 0}

    from_date = date.today() - timedelta(days=days_back)
    to_date = date.today()

    # Try BSE API first, fall back to insiderscreener
    raw_records = await fetch_from_bse_api(from_date, to_date)
    source = "bse_api"

    if not raw_records:
        log.info("falling_back_to_insiderscreener")
        raw_records = await fetch_from_insiderscreener()
        source = "insiderscreener"

    log.info("insider_records_raw", count=len(raw_records), source=source)

    for raw in raw_records:
        results["fetched"] += 1
        try:
            # Parse based on source
            if source == "bse_api":
                record = parse_bse_record(raw)
            else:
                record = parse_insiderscreener_record(raw)

            if not record or not record.get("isin") or not record.get("ticker"):
                results["skipped"] += 1
                continue

            # Skip if already stored
            if await is_duplicate(db, record):
                results["skipped"] += 1
                continue

            await db.table("india_promoter_signals").insert(record).execute()
            results["new"] += 1

        except Exception as e:
            log.error("store_insider_failed", error=str(e)[:120])
            results["errors"] += 1

    log.info("insider_fetch_complete", **results)
    return results
