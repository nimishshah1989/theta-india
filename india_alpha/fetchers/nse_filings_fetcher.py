"""
nse_filings_fetcher.py
Fetches corporate announcements from NSE's free public API.
Replaces StockInsights.ai (paid) with richer, free data.

NSE API:
  GET https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={SYMBOL}
  No auth required — just session cookies from NSE homepage.
  Returns: announcements with subject, PDF attachments, dates, categories.

Filing categories covered: concall transcripts, board outcomes, investor presentations,
acquisitions, press releases, credit ratings, dividends, management changes, auditor changes,
SEBI takeover disclosures, ESOPs, and general updates.
"""

import asyncio
import re
import warnings
import structlog
from datetime import datetime, date, timedelta
from io import BytesIO
from typing import Optional
import httpx

# Suppress noisy pdfplumber/pdfminer warnings about invalid color values
warnings.filterwarnings("ignore", message=".*non-stroke color.*")
warnings.filterwarnings("ignore", message=".*invalid float value.*")

log = structlog.get_logger()

NSE_BASE = "https://www.nseindia.com"
NSE_API = f"{NSE_BASE}/api/corporate-announcements"
SYMBOL_DELAY_SEC = 2.0
PDF_DELAY_SEC = 0.5
MAX_RETRIES = 3

# Category classification map
# Maps NSE desc/category keywords to our bucket + priority system
CATEGORY_MAP = {
    # earnings_strategy bucket
    "concall": {"bucket": "earnings_strategy", "priority": "HIGH", "download_pdf": True},
    "analyst meet": {"bucket": "earnings_strategy", "priority": "HIGH", "download_pdf": True},
    "earnings call": {"bucket": "earnings_strategy", "priority": "HIGH", "download_pdf": True},
    "board meeting": {"bucket": "earnings_strategy", "priority": "HIGH", "download_pdf": True},
    "outcome of board": {"bucket": "earnings_strategy", "priority": "HIGH", "download_pdf": True},
    "financial results": {"bucket": "earnings_strategy", "priority": "HIGH", "download_pdf": True},
    "investor presentation": {"bucket": "earnings_strategy", "priority": "MEDIUM", "download_pdf": True},
    "investors presentation": {"bucket": "earnings_strategy", "priority": "MEDIUM", "download_pdf": True},
    "annual report": {"bucket": "earnings_strategy", "priority": "MEDIUM", "download_pdf": True},

    # capital_action bucket
    "acquisition": {"bucket": "capital_action", "priority": "HIGH", "download_pdf": True},
    "amalgamation": {"bucket": "capital_action", "priority": "HIGH", "download_pdf": True},
    "merger": {"bucket": "capital_action", "priority": "HIGH", "download_pdf": True},
    "press release": {"bucket": "capital_action", "priority": "MEDIUM", "download_pdf": False},
    "credit rating": {"bucket": "capital_action", "priority": "MEDIUM", "download_pdf": False},
    "dividend": {"bucket": "capital_action", "priority": "MEDIUM", "download_pdf": False},
    "bonus": {"bucket": "capital_action", "priority": "MEDIUM", "download_pdf": False},
    "buyback": {"bucket": "capital_action", "priority": "MEDIUM", "download_pdf": True},
    "split": {"bucket": "capital_action", "priority": "MEDIUM", "download_pdf": False},
    "esop": {"bucket": "capital_action", "priority": "LOW", "download_pdf": False},
    "esos": {"bucket": "capital_action", "priority": "LOW", "download_pdf": False},

    # governance bucket
    "appointment": {"bucket": "governance", "priority": "MEDIUM", "download_pdf": False},
    "cessation": {"bucket": "governance", "priority": "MEDIUM", "download_pdf": False},
    "resignation": {"bucket": "governance", "priority": "MEDIUM", "download_pdf": False},
    "change in director": {"bucket": "governance", "priority": "MEDIUM", "download_pdf": False},
    "auditor": {"bucket": "governance", "priority": "HIGH", "download_pdf": False},
    "change in auditor": {"bucket": "governance", "priority": "HIGH", "download_pdf": False},
    "takeover": {"bucket": "governance", "priority": "MEDIUM", "download_pdf": False},
    "regulation 29": {"bucket": "governance", "priority": "MEDIUM", "download_pdf": False},
    "general update": {"bucket": "governance", "priority": "LOW", "download_pdf": False},
    "updates": {"bucket": "governance", "priority": "LOW", "download_pdf": False},

    # procedural — SKIP
    "newspaper": {"bucket": "procedural", "priority": "SKIP", "download_pdf": False},
    "advertisement": {"bucket": "procedural", "priority": "SKIP", "download_pdf": False},
    "certificate": {"bucket": "procedural", "priority": "SKIP", "download_pdf": False},
    "trading window": {"bucket": "procedural", "priority": "SKIP", "download_pdf": False},
    "shareholders meeting": {"bucket": "procedural", "priority": "SKIP", "download_pdf": False},
    "annual general meeting": {"bucket": "procedural", "priority": "SKIP", "download_pdf": False},
    "postal ballot": {"bucket": "procedural", "priority": "SKIP", "download_pdf": False},
    "book closure": {"bucket": "procedural", "priority": "SKIP", "download_pdf": False},
    "record date": {"bucket": "procedural", "priority": "SKIP", "download_pdf": False},
    "listing": {"bucket": "procedural", "priority": "SKIP", "download_pdf": False},
    "compliance certificate": {"bucket": "procedural", "priority": "SKIP", "download_pdf": False},
}


def classify_filing(subject: str, category: str) -> dict:
    """
    Map NSE filing subject/category to our bucket + priority.
    Matches longest keywords first so specific patterns beat generic ones.
    Returns: {"category": str, "bucket": str, "priority": str, "download_pdf": bool}
    """
    combined = f"{subject} {category}".lower()

    # Sort by keyword length descending — "outcome of board" before "board meeting"
    for keyword in sorted(CATEGORY_MAP.keys(), key=len, reverse=True):
        if keyword in combined:
            return {
                "category": keyword,
                **CATEGORY_MAP[keyword],
            }

    # Default: governance/LOW for unrecognised filings
    return {
        "category": "other",
        "bucket": "governance",
        "priority": "LOW",
        "download_pdf": False,
    }


async def get_nse_session(client: httpx.AsyncClient) -> None:
    """
    Hit NSE homepage to establish session cookies.
    NSE requires valid cookies for API calls (same pattern as BSE insider).
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }
    try:
        resp = await client.get(NSE_BASE, headers=headers, follow_redirects=True)
        log.info("nse_session_established", status=resp.status_code,
                 cookies=len(client.cookies))
    except Exception as exc:
        log.warning("nse_session_failed", error=str(exc)[:100])


async def fetch_filings_for_symbol(
    client: httpx.AsyncClient,
    symbol: str,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> list[dict]:
    """
    Fetch all corporate announcements for one NSE symbol.
    Returns list of raw filing dicts from NSE API.
    """
    if to_date is None:
        to_date = date.today()
    if from_date is None:
        from_date = to_date - timedelta(days=425)  # ~14 months

    params = {
        "index": "equities",
        "symbol": symbol,
        "from_date": from_date.strftime("%d-%m-%Y"),
        "to_date": to_date.strftime("%d-%m-%Y"),
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{NSE_BASE}/companies-listing/corporate-filings-announcements",
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(NSE_API, params=params, headers=headers, timeout=30)

            if resp.status_code == 403:
                # Session expired — re-establish
                log.warning("nse_403_reauth", symbol=symbol, attempt=attempt + 1)
                await get_nse_session(client)
                await asyncio.sleep(2 ** (attempt + 1))
                continue

            if resp.status_code == 429:
                wait = 2 ** (attempt + 2)
                log.warning("nse_rate_limited", symbol=symbol, retry_in=wait)
                await asyncio.sleep(wait)
                continue

            if resp.status_code != 200:
                log.warning("nse_api_error", symbol=symbol, status=resp.status_code)
                return []

            data = resp.json()
            # NSE returns a list of announcements directly
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get("data", data.get("announcements", []))
            return []

        except httpx.TimeoutException:
            log.warning("nse_timeout", symbol=symbol, attempt=attempt + 1)
            await asyncio.sleep(2 ** attempt)
        except Exception as exc:
            log.error("nse_fetch_error", symbol=symbol, error=str(exc)[:100])
            return []

    return []


def _extract_pdf_url(raw: dict) -> Optional[str]:
    """Extract PDF attachment URL from NSE filing record."""
    # NSE uses attchmntFile or attchmntText fields
    attachment = raw.get("attchmntFile") or raw.get("an_attachment") or ""
    if attachment and attachment.strip():
        if attachment.startswith("http"):
            return attachment
        # Relative path — construct full URL
        return f"https://archives.nseindia.com/corporate/ann/{attachment}"
    return None


def _extract_pdf_size(raw: dict) -> Optional[int]:
    """Extract PDF file size if available."""
    size = raw.get("attchmntSize") or raw.get("file_size")
    if size:
        try:
            return int(size)
        except (ValueError, TypeError):
            pass
    return None


async def download_and_extract_pdf(
    client: httpx.AsyncClient,
    pdf_url: str,
    max_size_mb: float = 25.0,
) -> tuple[str, str]:
    """
    Download PDF and extract text using pdfplumber.
    Returns (extracted_text, extraction_method).
    Uses BytesIO — no temp files needed.
    """
    try:
        import pdfplumber
    except ImportError:
        log.warning("pdfplumber_not_installed",
                    msg="pip install pdfplumber")
        return ("", "failed")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/pdf,*/*",
        "Referer": NSE_BASE,
    }

    for attempt in range(2):
        try:
            resp = await client.get(pdf_url, headers=headers, timeout=60,
                                    follow_redirects=True)

            if resp.status_code != 200:
                log.warning("pdf_download_failed", url=pdf_url[:80],
                            status=resp.status_code)
                return ("", "failed")

            # Size check
            content_length = len(resp.content)
            if content_length > max_size_mb * 1024 * 1024:
                log.warning("pdf_too_large", url=pdf_url[:80],
                            size_mb=round(content_length / (1024 * 1024), 1))
                return ("", "failed")

            # Extract text
            pdf_bytes = BytesIO(resp.content)
            text_parts = []

            with pdfplumber.open(pdf_bytes) as pdf:
                for page in pdf.pages[:50]:  # Cap at 50 pages
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)

            full_text = "\n\n".join(text_parts).strip()

            if not full_text:
                return ("", "failed")

            return (full_text, "pdfplumber")

        except Exception as exc:
            log.warning("pdf_extract_error", url=pdf_url[:80],
                        error=str(exc)[:100], attempt=attempt + 1)
            if attempt == 0:
                await asyncio.sleep(1)

    return ("", "failed")


def _parse_nse_date(date_str: str) -> Optional[str]:
    """Parse NSE date formats into ISO format."""
    if not date_str:
        return None

    # NSE uses various formats: "09-Mar-2026", "09 Mar 2026", "2026-03-09"
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.isoformat()
        except ValueError:
            continue

    return None


def _parse_sort_date(date_str: str) -> Optional[str]:
    """Parse NSE date to just DATE (YYYY-MM-DD) for sort_date column."""
    if not date_str:
        return None

    for fmt in ("%d-%b-%Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None


async def fetch_and_store_filings(
    db,
    symbols: Optional[list[dict]] = None,
    months_back: int = 14,
    max_companies: int = 100,
    fetch_all: bool = False,
    skip_pdf_download: bool = False,
) -> dict:
    """
    Main entry point. Fetches NSE corporate filings for qualified companies.
    If symbols not provided and fetch_all=False, fetches for companies with
    promoter/OL score >= 25. If fetch_all=True, fetches for all companies.

    Args:
        db: Async Supabase client
        symbols: List of {"ticker": str, "isin": str} dicts. If None, auto-selected.
        months_back: How far back to look (default 14 months)
        max_companies: Max companies to process in one run
        fetch_all: If True, fetch for all companies regardless of scores
        skip_pdf_download: If True, skip PDF downloads (metadata only) for speed

    Returns:
        {"fetched": int, "stored": int, "pdfs_downloaded": int, "pdfs_failed": int, "skipped": int, "errors": int}
    """
    results = {
        "fetched": 0,
        "stored": 0,
        "pdfs_downloaded": 0,
        "pdfs_failed": 0,
        "skipped_procedural": 0,
        "skipped_duplicate": 0,
        "skipped_resume": 0,
        "errors": 0,
        "companies_processed": 0,
    }

    # Build symbol list
    if not symbols:
        if fetch_all:
            # Fetch for ALL companies in the universe
            from india_alpha.db import fetch_all_rows
            all_companies = await fetch_all_rows(
                db, "india_companies", select="isin, ticker"
            )
            symbols = [
                {"ticker": c["ticker"], "isin": c["isin"]}
                for c in all_companies
                if c.get("ticker") and c.get("isin")
            ]
            symbols = symbols[:max_companies]
        else:
            # Original behavior: only companies with meaningful scores
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
                    symbol_list.append({"ticker": row["ticker"], "isin": row["isin"]})
            symbols = symbol_list[:max_companies]
    else:
        symbols = symbols[:max_companies]

    # Resume support: find companies that already have filings in last N months
    if fetch_all:
        cutoff_date = (date.today() - timedelta(days=months_back * 30)).isoformat()
        try:
            from india_alpha.db import fetch_all_rows as _fetch_all
            existing_filings = await _fetch_all(
                db, "india_corporate_filings", select="ticker"
            )
            tickers_with_filings = {r["ticker"] for r in existing_filings}
            original_count = len(symbols)
            symbols = [s for s in symbols if s["ticker"] not in tickers_with_filings]
            results["skipped_resume"] = original_count - len(symbols)
            if results["skipped_resume"] > 0:
                log.info("corporate_filings_resume",
                         skipped=results["skipped_resume"],
                         remaining=len(symbols))
        except Exception as exc:
            log.warning("resume_check_failed", error=str(exc)[:100])

    log.info("corporate_filings_fetch_start", companies=len(symbols))

    from_date = date.today() - timedelta(days=months_back * 30)
    to_date = date.today()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Establish NSE session first
        await get_nse_session(client)
        await asyncio.sleep(1)

        for company in symbols:
            ticker = company["ticker"]
            isin = company["isin"]

            try:
                # Look up company name
                comp_result = await db.table("india_companies") \
                    .select("company_name") \
                    .eq("ticker", ticker) \
                    .execute()
                company_name = comp_result.data[0]["company_name"] if comp_result.data else ticker

                # Pre-fetch existing seq_ids for this company (avoid N+1 queries)
                existing_result = await db.table("india_corporate_filings") \
                    .select("nse_seq_id") \
                    .eq("isin", isin) \
                    .execute()
                existing_seq_ids = {r["nse_seq_id"] for r in (existing_result.data or [])}

                raw_filings = await fetch_filings_for_symbol(
                    client, ticker, from_date, to_date
                )
                results["fetched"] += len(raw_filings)

                for raw in raw_filings:
                    try:
                        subject = raw.get("desc") or raw.get("subject") or ""
                        nse_category = raw.get("smIndustry") or raw.get("category") or ""
                        nse_seq_id = str(raw.get("seq_id") or raw.get("an_dt", ""))
                        ann_date_str = raw.get("an_dt") or raw.get("dt") or ""
                        sort_date_str = raw.get("sort_date") or ann_date_str

                        if not nse_seq_id:
                            continue

                        # Fast duplicate check using pre-fetched set
                        if nse_seq_id in existing_seq_ids:
                            results["skipped_duplicate"] += 1
                            continue

                        # Classify filing
                        classification = classify_filing(subject, nse_category)

                        # Skip procedural filings
                        if classification["priority"] == "SKIP":
                            results["skipped_procedural"] += 1
                            continue

                        # Parse dates
                        announcement_date = _parse_nse_date(ann_date_str)
                        sort_date = _parse_sort_date(sort_date_str) or _parse_sort_date(ann_date_str)

                        if not announcement_date:
                            announcement_date = datetime.now().isoformat()
                        if not sort_date:
                            sort_date = date.today().isoformat()

                        # Extract PDF URL
                        pdf_url = _extract_pdf_url(raw)
                        pdf_size = _extract_pdf_size(raw)

                        # Build filing record
                        record = {
                            "isin": isin,
                            "ticker": ticker,
                            "company_name": company_name,
                            "nse_seq_id": nse_seq_id,
                            "announcement_date": announcement_date,
                            "sort_date": sort_date,
                            "category": classification["category"],
                            "category_bucket": classification["bucket"],
                            "signal_priority": classification["priority"],
                            "subject_text": subject[:2000] if subject else None,
                            "pdf_url": pdf_url,
                            "pdf_size_bytes": pdf_size,
                            "raw_json": raw,
                            "fetched_at": datetime.now().isoformat(),
                        }

                        # Download PDF for HIGH/MEDIUM priority filings (unless skip_pdf_download)
                        if classification["download_pdf"] and pdf_url and not skip_pdf_download:
                            extracted_text, method = await download_and_extract_pdf(
                                client, pdf_url
                            )
                            record["extracted_text"] = extracted_text or None
                            record["word_count"] = len(extracted_text.split()) if extracted_text else 0
                            record["extraction_method"] = method
                            record["is_downloaded"] = bool(extracted_text)
                            record["is_text_extracted"] = bool(extracted_text)

                            if extracted_text:
                                results["pdfs_downloaded"] += 1
                            else:
                                results["pdfs_failed"] += 1

                            await asyncio.sleep(PDF_DELAY_SEC)
                        else:
                            # Subject-only extraction for non-PDF filings
                            record["extracted_text"] = subject
                            record["word_count"] = len(subject.split()) if subject else 0
                            record["extraction_method"] = "subject_only"
                            record["is_downloaded"] = False
                            record["is_text_extracted"] = True

                        # Store filing
                        await db.table("india_corporate_filings").insert(record).execute()
                        results["stored"] += 1

                    except Exception as exc:
                        log.error("filing_store_failed",
                                  ticker=ticker,
                                  error=str(exc)[:120])
                        results["errors"] += 1

                results["companies_processed"] += 1

            except Exception as exc:
                log.error("company_filings_failed",
                          ticker=ticker, error=str(exc)[:120])
                results["errors"] += 1

            # Rate limiting between symbols
            await asyncio.sleep(SYMBOL_DELAY_SEC)

    log.info("corporate_filings_fetch_complete", **results)
    return results
