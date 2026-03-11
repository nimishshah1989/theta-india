"""
universe_builder.py
Builds the india_companies master registry from NSE's free symbol list.
NSE publishes EQUITY_L.csv daily — all listed equity symbols with ISIN.
Enriched with yfinance for market cap, sector, fundamentals.
Target: 1,500–2,000 companies classified by tier.
"""

import asyncio
import csv
import io
from datetime import date
from typing import Optional

import httpx
import structlog
import yfinance as yf

log = structlog.get_logger()

NSE_EQUITY_CSV = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

# Universe filters — India mid/small/micro is the alpha hunting ground
MIN_MARKET_CAP_CR = 100      # ₹100 Cr floor (below this, liquidity too thin)
MAX_MARKET_CAP_CR = 75000    # ₹75,000 Cr ceiling (exclude mega-caps)

# Sectors to skip — different valuation frameworks
EXCLUDED_SECTORS = {"Financial Services", "Real Estate"}


def get_market_cap_tier(mcap_cr: float) -> str:
    if mcap_cr >= 20000: return "LARGE"
    if mcap_cr >= 5000:  return "MID"
    if mcap_cr >= 500:   return "SMALL"
    if mcap_cr >= 50:    return "MICRO"
    return "NANO"


async def fetch_nse_symbols() -> list[dict]:
    """Download and parse NSE's daily equity symbol CSV."""
    log.info("fetching_nse_symbol_list")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.nseindia.com/",
        "Accept-Language": "en-US,en;q=0.5",
    }
    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
        response = await client.get(NSE_EQUITY_CSV)
        response.raise_for_status()

    # Use csv.reader to handle quoted fields (company names with commas)
    reader = csv.reader(io.StringIO(response.text.strip()))
    headers_row = [h.strip() for h in next(reader)]
    log.info("nse_csv_headers", headers=headers_row)

    symbols = []
    for parts in reader:
        if len(parts) < 3:
            continue
        symbol = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else symbol
        series = parts[2].strip() if len(parts) > 2 else "EQ"
        # ISIN NUMBER is at index 6 (7th column), NOT the last column
        isin = parts[6].strip() if len(parts) > 6 else ""

        if series == "EQ" and symbol:
            symbols.append({
                "nse_symbol": symbol,
                "company_name": name,
                "isin": isin,
            })

    log.info("nse_symbols_parsed", count=len(symbols))
    return symbols


def enrich_symbol_sync(sym: dict) -> Optional[dict]:
    """
    Fetch yfinance data for one NSE symbol.
    Synchronous — called from thread pool to avoid blocking async loop.
    Returns None if symbol fails filters or data unavailable.
    """
    ticker_str = f"{sym['nse_symbol']}.NS"
    try:
        ticker = yf.Ticker(ticker_str)
        info = ticker.info

        if not info or not isinstance(info, dict):
            return None

        # Must be equity
        if info.get("quoteType", "") not in ("EQUITY", ""):
            return None

        market_cap = info.get("marketCap", 0) or 0
        market_cap_cr = market_cap / 10_000_000

        # Apply filters
        if market_cap_cr < MIN_MARKET_CAP_CR or market_cap_cr > MAX_MARKET_CAP_CR:
            return None

        sector = info.get("sector", "") or ""
        if sector in EXCLUDED_SECTORS:
            return None

        revenue = info.get("totalRevenue", 0) or 0
        net_income = info.get("netIncomeToCommon", 0) or 0

        return {
            "ticker": sym["nse_symbol"],
            "exchange": "NSE",
            "nse_symbol": sym["nse_symbol"],
            "company_name": sym.get("company_name") or info.get("longName", sym["nse_symbol"]),
            "isin": sym.get("isin", ""),
            "sector": sector,
            "industry": info.get("industry", "") or "",
            "market_cap_cr": round(market_cap_cr, 1),
            "market_cap_tier": get_market_cap_tier(market_cap_cr),
            "revenue_cr_ttm": round(revenue / 10_000_000, 1),
            "revenue_growth_yoy": info.get("revenueGrowth"),
            "ebitda_margin": info.get("ebitdaMargins"),
            "pat_cr_ttm": round(net_income / 10_000_000, 1),
            "roe": info.get("returnOnEquity"),
            "debt_equity": info.get("debtToEquity"),
            "analyst_count": info.get("numberOfAnalystOpinions") or 0,
            "is_f_and_o": False,
            "is_index_stock": False,
            # Valuation fields (used by valuation_scorer)
            "trailing_pe": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "price_to_book": info.get("priceToBook"),
            "ev_to_ebitda": info.get("enterpriseToEbitda"),
            "dividend_yield": info.get("dividendYield"),
            "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
            "two_hundred_dma": info.get("twoHundredDayAverage"),
            "free_cash_flow": round((info.get("freeCashflow") or 0) / 10_000_000, 2) if info.get("freeCashflow") else None,
            "first_seen": date.today().isoformat(),
        }
    except Exception as e:
        log.debug("yfinance_failed", ticker=ticker_str, error=str(e)[:80])
        return None


async def build_universe(db, max_symbols: int = 2000, batch_size: int = 10) -> dict:
    """
    Main entry point. Fetches NSE symbols, enriches with yfinance, stores in DB.
    max_symbols: default 2000 covers full NSE equity universe after filters.
    """
    results = {"fetched": 0, "enriched": 0, "stored": 0, "failed": 0}

    symbols = await fetch_nse_symbols()
    symbols = symbols[:max_symbols]  # Cap for this run
    log.info("universe_build_start", total_symbols=len(symbols))

    loop = asyncio.get_running_loop()

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        tasks = [
            loop.run_in_executor(None, enrich_symbol_sync, sym)
            for sym in batch
        ]
        enriched_batch = await asyncio.gather(*tasks, return_exceptions=True)

        for enriched in enriched_batch:
            results["fetched"] += 1
            if isinstance(enriched, Exception) or enriched is None:
                results["failed"] += 1
                continue

            results["enriched"] += 1
            try:
                await db.table("india_companies").upsert(
                    enriched, on_conflict="exchange,ticker"
                ).execute()
                results["stored"] += 1
            except Exception as e:
                log.error("store_company_failed",
                          ticker=enriched.get("ticker"), error=str(e)[:120])
                results["failed"] += 1

        progress = min(i + batch_size, len(symbols))
        log.info("universe_progress",
                 processed=progress, total=len(symbols),
                 enriched=results["enriched"])
        await asyncio.sleep(1.5)  # Rate limiting — avoid Yahoo Finance blocks

    log.info("universe_build_complete", **results)
    return results
