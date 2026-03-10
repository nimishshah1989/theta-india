"""
nse_bulk_deals_fetcher.py
Fetches bulk and block deals from NSE's free public API.

Source: NSE Snapshot Large Deals endpoint
  GET https://www.nseindia.com/api/snapshot-capital-market-largedeal
  No auth required — just session cookies from NSE homepage.
  Returns JSON with keys BULK_DEALS_DATA and BLOCK_DEALS_DATA.

Methodology:
  - Fetches all recent bulk/block deals from NSE
  - Matches client names against a curated list of superstar investors
    (Kacholia, Kedia, Damani, Jhunjhunwala legacy, etc.)
  - Flags institutional deals (mutual funds, FIIs, insurance, banks)
  - Stores in india_bulk_deals table with superstar/institutional tags
  - Used by the promoter signal layer to detect smart money accumulation
"""

import asyncio
import json
import structlog
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import httpx

log = structlog.get_logger()

NSE_BASE = "https://www.nseindia.com"
NSE_DEALS_API = f"{NSE_BASE}/api/snapshot-capital-market-largedeal"
MAX_RETRIES = 2
RETRY_DELAY_SEC = 3.0

DATA_DIR = Path(__file__).parent.parent / "data"

# --- Helper loaders ---


def _load_superstars() -> tuple[list[dict], list[str]]:
    """Load superstar investor aliases and institutional keywords from JSON."""
    with open(DATA_DIR / "superstar_investors.json") as f:
        data = json.load(f)
    return data["investors"], data["institutional_keywords"]


# --- NSE session ---


async def _get_nse_session(client: httpx.AsyncClient) -> None:
    """
    Hit NSE homepage to populate session cookies on the client.
    NSE requires valid cookies before any API call will succeed.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.nseindia.com/",
    }
    try:
        resp = await client.get(NSE_BASE, headers=headers, follow_redirects=True)
        log.info("nse_deals_session_established", status=resp.status_code,
                 cookies=len(client.cookies))
    except Exception as exc:
        log.warning("nse_deals_session_failed", error=str(exc)[:100])


# --- Matching functions ---


def _is_superstar(client_name: str, investors: list[dict]) -> tuple[bool, Optional[str]]:
    """
    Check if client_name matches any superstar investor alias.
    Uses case-insensitive substring matching against all known aliases.
    Returns (True, canonical_investor_name) or (False, None).
    """
    name_lower = client_name.lower()
    for investor in investors:
        for alias in investor["aliases"]:
            if alias.lower() in name_lower:
                return True, investor["name"]
    return False, None


def _is_institutional(client_name: str, keywords: list[str]) -> bool:
    """
    Check if client_name contains any institutional keyword.
    Catches mutual funds, FIIs, insurance companies, banks, etc.
    """
    name_lower = client_name.lower()
    for keyword in keywords:
        if keyword.lower() in name_lower:
            return True
    return False


# --- Deal parser ---


def _parse_deal(
    raw: dict,
    deal_type: str,
    investors: list[dict],
    keywords: list[str],
) -> Optional[dict]:
    """
    Parse one deal record from NSE response into our standard format.
    Returns dict ready for DB insertion, or None if parsing fails.
    """
    try:
        ticker = raw.get("symbol", "").strip()
        if not ticker:
            return None

        # Parse trade date from "DD-Mon-YYYY" format
        date_str = raw.get("date", "").strip()
        if not date_str:
            return None
        trade_date = datetime.strptime(date_str, "%d-%b-%Y").date()

        client_name = raw.get("clientName", "").strip()
        if not client_name:
            return None

        # Normalize buy/sell direction
        buy_sell_raw = raw.get("buySell", "").strip().upper()
        if buy_sell_raw not in ("BUY", "SELL"):
            # Try to infer from common variations
            if "BUY" in buy_sell_raw:
                buy_sell_raw = "BUY"
            elif "SELL" in buy_sell_raw:
                buy_sell_raw = "SELL"
            else:
                return None

        # Parse quantity — NSE uses "qty" (fallback to "quantityTraded" for compat)
        quantity_raw = str(raw.get("qty") or raw.get("quantityTraded") or "0").replace(",", "").strip()
        quantity = int(float(quantity_raw))

        # Parse price — NSE uses "watp" (weighted avg traded price, fallback to "tradedPrice")
        price_raw = str(raw.get("watp") or raw.get("tradedPrice") or "0").replace(",", "").strip()
        price = float(price_raw)

        # Value in crores (1 crore = 1,00,00,000 = 10^7)
        value_cr = (quantity * price) / 10_000_000

        # Superstar and institutional flags
        is_star, star_name = _is_superstar(client_name, investors)
        is_inst = _is_institutional(client_name, keywords)

        return {
            "ticker": ticker,
            "trade_date": trade_date.isoformat(),
            "deal_type": deal_type,
            "client_name": client_name,
            "buy_sell": buy_sell_raw,
            "quantity": quantity,
            "price": price,
            "value_cr": round(value_cr, 4),
            "is_superstar": is_star,
            "superstar_name": star_name,
            "is_institutional": is_inst,
        }

    except Exception as exc:
        log.warning("deal_parse_failed", error=str(exc)[:100],
                    raw_symbol=raw.get("symbol", "?"))
        return None


# --- Main entry point ---


async def fetch_and_store_bulk_deals(db, days_back: int = 30) -> dict:
    """
    Fetch bulk and block deals from NSE, match against superstar investors,
    and store in india_bulk_deals table.

    Args:
        db: Async Supabase client
        days_back: Only store deals from the last N days

    Returns:
        Summary dict with fetched/stored/superstar_deals/errors counts
    """
    results = {"fetched": 0, "stored": 0, "superstar_deals": 0, "errors": 0}

    # Load superstar investor data
    investors, keywords = _load_superstars()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.nseindia.com/",
    }

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        # Establish NSE session first
        await _get_nse_session(client)
        await asyncio.sleep(1)

        # Fetch deals with retry logic
        raw_data = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await client.get(NSE_DEALS_API, headers=headers)

                if resp.status_code == 403:
                    log.warning("nse_deals_403_reauth", attempt=attempt + 1)
                    await _get_nse_session(client)
                    await asyncio.sleep(RETRY_DELAY_SEC)
                    continue

                if resp.status_code != 200:
                    log.warning("nse_deals_api_error", status=resp.status_code,
                                attempt=attempt + 1)
                    await asyncio.sleep(RETRY_DELAY_SEC)
                    continue

                raw_data = resp.json()
                break

            except httpx.TimeoutException:
                log.warning("nse_deals_timeout", attempt=attempt + 1)
                await asyncio.sleep(RETRY_DELAY_SEC)
            except Exception as exc:
                log.error("nse_deals_fetch_error", error=str(exc)[:100],
                          attempt=attempt + 1)
                await asyncio.sleep(RETRY_DELAY_SEC)

        if not raw_data:
            log.warning("nse_deals_no_data", msg="All retries exhausted or empty response")
            return results

        # Parse both deal types
        cutoff_date = date.today() - timedelta(days=days_back)
        parsed_deals: list[dict] = []

        for deal_type_key, deal_type_label in [
            ("BULK_DEALS_DATA", "BULK"),
            ("BLOCK_DEALS_DATA", "BLOCK"),
        ]:
            raw_deals = raw_data.get(deal_type_key, []) or []
            for raw_deal in raw_deals:
                parsed = _parse_deal(raw_deal, deal_type_label, investors, keywords)
                if parsed is None:
                    continue

                # Filter by date window
                trade_dt = date.fromisoformat(parsed["trade_date"])
                if trade_dt < cutoff_date:
                    continue

                parsed_deals.append(parsed)

        results["fetched"] = len(parsed_deals)
        log.info("nse_deals_parsed", total=len(parsed_deals))

        if not parsed_deals:
            return results

        # Build ticker-to-ISIN lookup from india_companies
        unique_tickers = list({deal["ticker"] for deal in parsed_deals})
        ticker_isin_map: dict[str, str] = {}

        # Supabase .in_() has practical limits, batch if needed
        batch_size = 50
        for i in range(0, len(unique_tickers), batch_size):
            batch = unique_tickers[i : i + batch_size]
            try:
                comp_result = await db.table("india_companies") \
                    .select("ticker, isin") \
                    .in_("ticker", batch) \
                    .execute()
                for row in (comp_result.data or []):
                    ticker_isin_map[row["ticker"]] = row["isin"]
            except Exception as exc:
                log.warning("isin_lookup_failed", error=str(exc)[:100])

        # Upsert deals to DB
        for deal in parsed_deals:
            try:
                isin = ticker_isin_map.get(deal["ticker"])
                if not isin:
                    # Skip deals for tickers not in our universe
                    continue

                record = {
                    **deal,
                    "isin": isin,
                    "fetched_at": datetime.now().isoformat(),
                }

                await db.table("india_bulk_deals") \
                    .upsert(record, on_conflict="ticker,trade_date,client_name,buy_sell") \
                    .execute()

                results["stored"] += 1
                if deal["is_superstar"]:
                    results["superstar_deals"] += 1

            except Exception as exc:
                log.warning("deal_store_failed", ticker=deal["ticker"],
                            error=str(exc)[:100])
                results["errors"] += 1

    log.info("nse_deals_fetch_complete", **results)
    return results
