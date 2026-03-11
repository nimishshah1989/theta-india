"""
screener_enricher.py
Enriches india_companies with reliable valuation metrics from Screener.in HTML pages.

Screener.in's JSON API (/api/company/) is no longer available (returns 404).
Instead, we parse the HTML company page at /company/{TICKER}/ which reliably
contains all snapshot metrics: Market Cap, PE, Book Value, 52-week range,
sector/industry, ROCE, ROE, dividend yield, and full financial tables.

This replaces yfinance as the primary source for valuation data.
Coverage: ~100% of NSE/BSE listed companies.
Rate limit: 1 request per 1.2 seconds (free tier, session cookie required).
"""

import asyncio
import re
from datetime import datetime
from typing import Optional

import httpx
import structlog

log = structlog.get_logger()

SCREENER_BASE = "https://www.screener.in"


def _parse_indian_number(text: str) -> Optional[float]:
    """
    Parse Indian-formatted numbers from Screener.in HTML.
    Handles: '19,26,475', '52.8', '1,424', '-1.07', '0.39 %', '₹ 413'
    Returns None if unparseable.
    """
    if not text:
        return None
    cleaned = text.replace("₹", "").replace(",", "").replace("%", "").replace("Cr.", "").strip()
    if not cleaned or cleaned == "--" or cleaned == "":
        return None
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def parse_screener_html(html: str) -> dict:
    """
    Parse a Screener.in company HTML page and extract all snapshot metrics.
    Returns a dict with standardised field names matching india_companies columns.
    Pure function — no DB or side effects.
    """
    result = {}

    # ── TOP RATIOS SECTION ──────────────────────────────────────────────
    ratios_section = re.search(r'id="top-ratios">(.*?)</ul>', html, re.DOTALL)
    if not ratios_section:
        return result

    items = re.findall(r'<li.*?>(.*?)</li>', ratios_section.group(1), re.DOTALL)

    for item in items:
        name_match = re.search(r'<span class="name">(.*?)</span>', item, re.DOTALL)
        if not name_match:
            continue
        name = re.sub(r'<.*?>', '', name_match.group(1)).strip()

        # Extract all number spans within the value
        numbers = re.findall(r'<span class="number">([^<]+)</span>', item)
        # Also get the full text value for fields without number spans
        value_match = re.search(r'class="nowrap[^"]*">(.*?)</span>', item, re.DOTALL)
        full_value = ""
        if value_match:
            full_value = re.sub(r'<.*?>', '', value_match.group(1)).strip()
            full_value = re.sub(r'\s+', ' ', full_value)

        if name == "Market Cap":
            if numbers:
                result["market_cap_cr"] = _parse_indian_number(numbers[0])

        elif name == "Current Price":
            if numbers:
                result["current_price"] = _parse_indian_number(numbers[0])

        elif name == "High / Low":
            # Two number spans: high and low
            if len(numbers) >= 2:
                result["fifty_two_week_high"] = _parse_indian_number(numbers[0])
                result["fifty_two_week_low"] = _parse_indian_number(numbers[1])
            elif len(numbers) == 1:
                result["fifty_two_week_high"] = _parse_indian_number(numbers[0])

        elif name == "Stock P/E":
            result["trailing_pe"] = _parse_indian_number(full_value)

        elif name == "Book Value":
            bv = _parse_indian_number(full_value)
            if bv and bv > 0:
                result["book_value"] = bv
                # Compute P/B if current price is already parsed
                if result.get("current_price") and result["current_price"] > 0:
                    result["price_to_book"] = round(
                        result["current_price"] / bv, 2
                    )

        elif name == "Dividend Yield":
            result["dividend_yield"] = _parse_indian_number(full_value)

        elif name == "ROCE":
            result["roce"] = _parse_indian_number(full_value)

        elif name == "ROE":
            result["roe_screener"] = _parse_indian_number(full_value)

        elif name == "Face Value":
            result["face_value"] = _parse_indian_number(full_value)

    # ── SECTOR / INDUSTRY ───────────────────────────────────────────────
    sector_links = re.findall(
        r'title="(Broad Sector|Sector|Broad Industry|Industry)"[^>]*>([^<]+)',
        html
    )
    for title, val in sector_links:
        clean_val = val.strip()
        if title == "Broad Sector":
            result["screener_broad_sector"] = clean_val
        elif title == "Sector":
            result["sector"] = clean_val
        elif title == "Industry":
            result["industry"] = clean_val

    # ── P/B fallback (if book_value was parsed after current_price) ─────
    if (
        "price_to_book" not in result
        and result.get("current_price")
        and result.get("book_value")
        and result["book_value"] > 0
    ):
        result["price_to_book"] = round(
            result["current_price"] / result["book_value"], 2
        )

    # ── MARKET CAP TIER ─────────────────────────────────────────────────
    mcap = result.get("market_cap_cr")
    if mcap is not None:
        if mcap >= 20000:
            result["market_cap_tier"] = "LARGE"
        elif mcap >= 5000:
            result["market_cap_tier"] = "MID"
        elif mcap >= 500:
            result["market_cap_tier"] = "SMALL"
        elif mcap >= 50:
            result["market_cap_tier"] = "MICRO"
        else:
            result["market_cap_tier"] = "NANO"

    return result


def parse_financial_tables(html: str) -> dict:
    """
    Parse the financial tables (Quarterly Results, Profit & Loss, Balance Sheet)
    from Screener.in HTML. Returns structured data for india_financials_history.

    This replaces the broken JSON API parsing in screener_fetcher.py.
    """
    tables = {}

    # Find each section's table
    section_patterns = [
        ("quarters", r'Quarterly Results.*?(<table[^>]*class="data-table[^"]*".*?</table>)'),
        ("annual", r'Profit &amp; Loss.*?(<table[^>]*class="data-table[^"]*".*?</table>)'),
        ("balance_sheet", r'Balance Sheet.*?(<table[^>]*class="data-table[^"]*".*?</table>)'),
    ]

    for key, pattern in section_patterns:
        match = re.search(pattern, html, re.DOTALL)
        if not match:
            continue

        table_html = match.group(1)

        # Parse headers (period dates)
        headers = re.findall(r'<th[^>]*>(.*?)</th>', table_html, re.DOTALL)
        headers = [re.sub(r'<.*?>', '', h).strip() for h in headers]

        # Parse rows
        rows = []
        for row_match in re.finditer(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL):
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row_match.group(1), re.DOTALL)
            cells = [re.sub(r'<.*?>', '', c).strip() for c in cells]
            if cells and len(cells) > 1:
                rows.append(cells)

        tables[key] = {"headers": headers, "rows": rows}

    return tables


async def fetch_screener_page(
    client: httpx.AsyncClient,
    ticker: str,
) -> Optional[str]:
    """Fetch a single Screener.in company HTML page. Returns HTML or None."""
    url = f"{SCREENER_BASE}/company/{ticker}/"
    try:
        resp = await client.get(url)
        if resp.status_code == 200:
            html = resp.text
            if "top-ratios" in html:
                return html
            else:
                log.debug("screener_page_no_ratios", ticker=ticker)
                return None
        elif resp.status_code == 403:
            log.warning("screener_auth_failed", ticker=ticker,
                        msg="Session cookie invalid or expired")
            return None
        else:
            log.debug("screener_page_failed", ticker=ticker,
                      status=resp.status_code)
            return None
    except Exception as e:
        log.debug("screener_request_error", ticker=ticker,
                  error=str(e)[:80])
        return None


async def enrich_company(
    db,
    client: httpx.AsyncClient,
    ticker: str,
    isin: str,
) -> bool:
    """
    Fetch Screener.in HTML for one company and update india_companies
    with valuation metrics. Returns True on success.
    """
    html = await fetch_screener_page(client, ticker)
    if not html:
        return False

    metrics = parse_screener_html(html)
    if not metrics:
        return False

    # Build update record — only include fields with actual values
    update = {}
    field_map = {
        "market_cap_cr": "market_cap_cr",
        "market_cap_tier": "market_cap_tier",
        "current_price": "current_price",
        "trailing_pe": "trailing_pe",
        "price_to_book": "price_to_book",
        "dividend_yield": "dividend_yield",
        "fifty_two_week_high": "fifty_two_week_high",
        "fifty_two_week_low": "fifty_two_week_low",
        "sector": "sector",
        "industry": "industry",
    }

    for src_key, db_key in field_map.items():
        val = metrics.get(src_key)
        if val is not None:
            update[db_key] = val

    if not update:
        return False

    try:
        await db.table("india_companies").update(update).eq(
            "ticker", ticker
        ).execute()
        return True
    except Exception as e:
        log.debug("screener_enrich_update_failed", ticker=ticker,
                  error=str(e)[:100])
        return False


async def enrich_all_companies(
    db,
    session_cookie: str,
    delay_seconds: float = 1.2,
) -> dict:
    """
    Main entry point. Fetches Screener.in HTML for every company in
    india_companies and updates valuation fields.

    Returns summary: {enriched, failed, skipped, total}.
    """
    results = {"total": 0, "enriched": 0, "failed": 0, "skipped": 0}

    if not session_cookie:
        log.warning("screener_enrich_skipped",
                    msg="No SCREENER_SESSION_COOKIE — skipping enrichment")
        return results

    # Get all companies from universe (paginated to bypass 1000-row limit)
    from india_alpha.db import fetch_all_rows
    companies = await fetch_all_rows(db, "india_companies", select="ticker, isin")
    results["total"] = len(companies)

    if not companies:
        log.warning("screener_enrich_no_companies")
        return results

    log.info("screener_enrichment_start", companies=len(companies))

    headers = {
        "Cookie": f"sessionid={session_cookie}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://www.screener.in/",
    }

    async with httpx.AsyncClient(
        timeout=20, headers=headers, follow_redirects=True
    ) as client:
        for i, company in enumerate(companies):
            ticker = company.get("ticker", "")
            isin = company.get("isin", "")

            if not ticker or not isin:
                results["skipped"] += 1
                continue

            success = await enrich_company(db, client, ticker, isin)
            if success:
                results["enriched"] += 1
            else:
                results["failed"] += 1

            # Rate limiting
            await asyncio.sleep(delay_seconds)

            # Progress logging every 25 companies
            if (i + 1) % 25 == 0:
                log.info("screener_enrichment_progress",
                         processed=i + 1,
                         total=len(companies),
                         enriched=results["enriched"],
                         failed=results["failed"])

    log.info("screener_enrichment_complete", **results)
    return results


async def enrich_and_add_missing(
    db,
    session_cookie: str,
    nse_symbols: list,
    delay_seconds: float = 1.2,
) -> dict:
    """
    Enrichment for companies that failed yfinance and aren't in the universe yet.
    Takes a list of NSE symbol dicts [{nse_symbol, company_name, isin}].
    For each, fetches Screener.in to get market cap and valuation data.
    If market cap passes filter, upserts to india_companies.

    This recovers the ~40% of companies that yfinance fails on.
    """
    from india_alpha.fetchers.universe_builder import MAX_MARKET_CAP_CR, MIN_MARKET_CAP_CR

    results = {"checked": 0, "added": 0, "filtered": 0, "failed": 0}

    if not session_cookie:
        return results

    headers = {
        "Cookie": f"sessionid={session_cookie}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://www.screener.in/",
    }

    log.info("screener_recovery_start", symbols=len(nse_symbols))

    async with httpx.AsyncClient(
        timeout=20, headers=headers, follow_redirects=True
    ) as client:
        for i, sym in enumerate(nse_symbols):
            ticker = sym.get("nse_symbol", "")
            isin = sym.get("isin", "")
            company_name = sym.get("company_name", ticker)

            if not ticker:
                continue

            results["checked"] += 1

            html = await fetch_screener_page(client, ticker)
            if not html:
                results["failed"] += 1
                await asyncio.sleep(delay_seconds)
                continue

            metrics = parse_screener_html(html)
            mcap = metrics.get("market_cap_cr")

            if mcap is None or mcap < MIN_MARKET_CAP_CR or mcap > MAX_MARKET_CAP_CR:
                results["filtered"] += 1
                await asyncio.sleep(delay_seconds)
                continue

            # Build company record
            record = {
                "ticker": ticker,
                "exchange": "NSE",
                "nse_symbol": ticker,
                "company_name": company_name,
                "isin": isin,
                "market_cap_cr": mcap,
                "market_cap_tier": metrics.get("market_cap_tier", "SMALL"),
                "sector": metrics.get("sector", ""),
                "industry": metrics.get("industry", ""),
                "current_price": metrics.get("current_price"),
                "trailing_pe": metrics.get("trailing_pe"),
                "price_to_book": metrics.get("price_to_book"),
                "dividend_yield": metrics.get("dividend_yield"),
                "fifty_two_week_high": metrics.get("fifty_two_week_high"),
                "fifty_two_week_low": metrics.get("fifty_two_week_low"),
                "first_seen": datetime.now().strftime("%Y-%m-%d"),
            }

            try:
                await db.table("india_companies").upsert(
                    record, on_conflict="exchange,ticker"
                ).execute()
                results["added"] += 1
            except Exception as e:
                log.debug("screener_recovery_store_failed",
                          ticker=ticker, error=str(e)[:100])
                results["failed"] += 1

            await asyncio.sleep(delay_seconds)

            if (i + 1) % 25 == 0:
                log.info("screener_recovery_progress",
                         checked=results["checked"],
                         added=results["added"],
                         total=len(nse_symbols))

    log.info("screener_recovery_complete", **results)
    return results
