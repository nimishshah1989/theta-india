"""
DEPRECATED — Use nse_filings_fetcher.py instead.

concall_fetcher.py
Fetches earnings call transcripts from StockInsights.ai API (paid, ₹4,000/yr).
Replaced by NSE Corporate Announcements API (free) in nse_filings_fetcher.py.
Kept for backward compatibility with existing india_concalls table data.

StockInsights.ai API:
  GET /api/in/v0/documents?document_type=earnings-transcript&ticker=NSE:{symbol}
  Auth: Bearer token (STOCKINSIGHTS_API_KEY)
  Rate limit: 1 req/sec with exponential backoff
"""

import asyncio
import structlog
from datetime import datetime
from typing import Optional
import httpx

log = structlog.get_logger()

STOCKINSIGHTS_BASE = "https://stockinsights.ai/api/in/v0"
REQUEST_DELAY_SEC = 1.0
MAX_RETRIES = 3


def _parse_quarter_label(date_str: str, title: str) -> str:
    """
    Extract quarter label (Q1FY27 format) from transcript metadata.
    Falls back to date-based calculation if title doesn't contain quarter info.
    """
    title_upper = (title or "").upper()

    # Try extracting from title directly (e.g., "Q3 FY2026" or "Q3FY26")
    import re
    match = re.search(r'Q([1-4])\s*FY\s*(\d{2,4})', title_upper)
    if match:
        q = match.group(1)
        fy = match.group(2)
        if len(fy) == 4:
            fy = fy[2:]
        return f"Q{q}FY{fy}"

    # Fall back to date-based quarter calculation
    if date_str:
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            month = dt.month
            year = dt.year

            # Indian financial year: Apr=Q1, Jul=Q2, Oct=Q3, Jan=Q4
            if month in (4, 5, 6):
                quarter = 1
                fy = year + 1
            elif month in (7, 8, 9):
                quarter = 2
                fy = year + 1
            elif month in (10, 11, 12):
                quarter = 3
                fy = year + 1
            else:
                quarter = 4
                fy = year

            return f"Q{quarter}FY{str(fy)[2:]}"
        except (ValueError, TypeError):
            pass

    return "UNKNOWN"


async def fetch_transcripts_for_symbol(
    client: httpx.AsyncClient,
    api_key: str,
    symbol: str,
    max_transcripts: int = 4,
) -> list[dict]:
    """
    Fetch recent earnings transcripts for one NSE symbol.
    Returns list of parsed transcript records ready for DB.
    """
    url = f"{STOCKINSIGHTS_BASE}/documents"
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {
        "document_type": "earnings-transcript",
        "ticker": f"NSE:{symbol}",
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = await client.get(url, headers=headers, params=params, timeout=30)

            if response.status_code == 429:
                wait = 2 ** (attempt + 1)
                log.warning("rate_limited", symbol=symbol, retry_in=wait)
                await asyncio.sleep(wait)
                continue

            if response.status_code == 401:
                log.error("auth_failed", symbol=symbol,
                          msg="Invalid STOCKINSIGHTS_API_KEY")
                return []

            if response.status_code != 200:
                log.warning("api_error", symbol=symbol,
                            status=response.status_code)
                return []

            data = response.json()
            documents = data.get("documents") or data.get("data") or []

            if not documents:
                log.debug("no_transcripts", symbol=symbol)
                return []

            # Parse into records (newest first, limit to max_transcripts)
            records = []
            for doc in documents[:max_transcripts]:
                transcript_text = doc.get("content") or doc.get("text") or ""
                title = doc.get("title") or ""
                date_str = doc.get("date") or doc.get("published_at") or ""
                quarter = _parse_quarter_label(date_str, title)

                if quarter == "UNKNOWN":
                    continue

                records.append({
                    "ticker": symbol,
                    "company_name": doc.get("company_name") or symbol,
                    "quarter": quarter,
                    "call_date": date_str[:10] if date_str else None,
                    "transcript_url": doc.get("url") or doc.get("source_url"),
                    "transcript_text": transcript_text,
                    "word_count": len(transcript_text.split()) if transcript_text else 0,
                    "is_processed": False,
                    "fetched_at": datetime.now().isoformat(),
                })

            return records

        except httpx.TimeoutException:
            log.warning("timeout", symbol=symbol, attempt=attempt + 1)
            await asyncio.sleep(2 ** attempt)
        except Exception as e:
            log.error("fetch_error", symbol=symbol, error=str(e)[:100])
            return []

    return []


async def fetch_and_store_concalls(
    db,
    api_key: str,
    symbols: Optional[list[str]] = None,
    max_transcripts_per_company: int = 2,
    max_companies: int = 50,
) -> dict:
    """
    Fetch concall transcripts for companies and store in india_concalls.
    If symbols not provided, fetches for companies with promoter/OL score >= 25.
    """
    results = {"fetched": 0, "stored": 0, "skipped": 0, "errors": 0}

    if not api_key:
        log.warning("concall_fetch_skipped",
                    msg="No STOCKINSIGHTS_API_KEY configured")
        return {**results, "skipped": True, "reason": "no_api_key"}

    # Build symbol list from companies with meaningful scores
    if not symbols:
        promo = await db.table("india_promoter_summary") \
            .select("isin, ticker") \
            .gte("promoter_signal_score", 25) \
            .execute()

        ol = await db.table("india_operating_leverage_scores") \
            .select("isin, ticker") \
            .gte("ol_score", 25) \
            .execute()

        seen = set()
        symbol_list = []
        for row in (promo.data or []) + (ol.data or []):
            if row["ticker"] not in seen:
                seen.add(row["ticker"])
                # Also need ISIN for DB writes
                symbol_list.append({"ticker": row["ticker"], "isin": row["isin"]})
        symbols_with_isin = symbol_list[:max_companies]
    else:
        # Look up ISINs for provided symbols
        symbols_with_isin = []
        for sym in symbols[:max_companies]:
            comp = await db.table("india_companies") \
                .select("isin, ticker") \
                .eq("ticker", sym) \
                .execute()
            if comp.data:
                symbols_with_isin.append(comp.data[0])
            else:
                symbols_with_isin.append({"ticker": sym, "isin": sym})

    log.info("concall_fetch_start", companies=len(symbols_with_isin))

    async with httpx.AsyncClient() as client:
        for company in symbols_with_isin:
            ticker = company["ticker"]
            isin = company["isin"]

            try:
                records = await fetch_transcripts_for_symbol(
                    client, api_key, ticker, max_transcripts_per_company
                )
                results["fetched"] += len(records)

                for record in records:
                    record["isin"] = isin

                    # Check if already stored (dedup by isin + quarter)
                    existing = await db.table("india_concalls") \
                        .select("id") \
                        .eq("isin", isin) \
                        .eq("quarter", record["quarter"]) \
                        .execute()

                    if existing.data:
                        results["skipped"] += 1
                        continue

                    await db.table("india_concalls").insert(record).execute()
                    results["stored"] += 1

            except Exception as e:
                log.error("concall_store_failed",
                          ticker=ticker, error=str(e)[:100])
                results["errors"] += 1

            # Rate limiting
            await asyncio.sleep(REQUEST_DELAY_SEC)

    log.info("concall_fetch_complete", **results)
    return results
