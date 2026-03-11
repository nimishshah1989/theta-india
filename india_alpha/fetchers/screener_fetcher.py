"""
screener_fetcher.py
Fetches 10-year financial history from Screener.in HTML pages.
Best free source for structured Indian financials.

NOTE: The Screener.in JSON API (/api/company/) is no longer available (returns 404).
This module now parses HTML tables from /company/{TICKER}/ pages instead.

Authentication: Requires session cookie from screener.in
Rate limit: ~1 request/second (free tier)
Coverage: All NSE/BSE listed companies

To get your session cookie:
  1. Log into screener.in in Chrome
  2. F12 → Application → Cookies → screener.in
  3. Copy the 'sessionid' value
  4. Add to .env as SCREENER_SESSION_COOKIE
"""

import asyncio
import re
from calendar import monthrange
from typing import Optional

import httpx
import structlog

log = structlog.get_logger()

SCREENER_BASE = "https://www.screener.in"

MONTH_MAP = {
    "Jan": "01", "Feb": "02", "Mar": "03",
    "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09",
    "Oct": "10", "Nov": "11", "Dec": "12",
}


def parse_screener_date(date_str: str) -> Optional[str]:
    """Convert 'Mar 2024' → '2024-03-31', 'TTM' → None."""
    if not date_str or date_str.strip() == "TTM":
        return None
    try:
        parts = str(date_str).strip().split()
        if len(parts) == 2:
            month_abbr = parts[0][:3]
            year = int(parts[1])
            if month_abbr in MONTH_MAP:
                month_num = int(MONTH_MAP[month_abbr])
                last_day = monthrange(year, month_num)[1]
                return f"{year}-{MONTH_MAP[month_abbr]}-{last_day:02d}"
    except Exception as e:
        log.debug("date_parse_failed", date_str=date_str, error=str(e))
    return None


def safe_float(val) -> Optional[float]:
    """Convert screener value to float, handling commas and % signs."""
    if val is None or val == "" or val == "--" or val == "—":
        return None
    try:
        cleaned = str(val).replace(",", "").replace("%", "").strip()
        return float(cleaned) if cleaned else None
    except (ValueError, TypeError):
        return None


def _parse_html_table(table_html: str) -> dict:
    """
    Parse a Screener.in HTML data-table into a structured dict.
    Returns {headers: [period strings], rows: {row_name: [values]}}.
    """
    result = {"headers": [], "rows": {}}

    # Extract headers from thead
    thead = re.search(r'<thead>(.*?)</thead>', table_html, re.DOTALL)
    if thead:
        headers = re.findall(r'<th[^>]*>(.*?)</th>', thead.group(1), re.DOTALL)
        result["headers"] = [
            re.sub(r'<.*?>', '', h).strip() for h in headers
        ]

    # Extract data rows from tbody
    tbody = re.search(r'<tbody>(.*?)</tbody>', table_html, re.DOTALL)
    if not tbody:
        return result

    for row_match in re.finditer(r'<tr[^>]*>(.*?)</tr>', tbody.group(1), re.DOTALL):
        row_html = row_match.group(1)
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, re.DOTALL)
        if not cells:
            continue

        # First cell is the row label — strip HTML (including multi-line button tags)
        row_name = re.sub(r'<[^>]+>', '', cells[0], flags=re.DOTALL)
        row_name = re.sub(r'&nbsp;', ' ', row_name).strip().rstrip('+').strip()
        # Clean values the same way
        values = [
            re.sub(r'<[^>]+>', '', c, flags=re.DOTALL).strip()
            for c in cells[1:]
        ]

        if row_name:
            result["rows"][row_name] = values

    return result


async def fetch_company_html(ticker: str, session_cookie: str) -> Optional[str]:
    """
    Fetch the Screener.in company HTML page.
    Returns HTML string or None if failed.
    """
    headers = {
        "Cookie": f"sessionid={session_cookie}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.screener.in/",
        "Accept": "text/html,application/xhtml+xml",
    }

    url = f"{SCREENER_BASE}/company/{ticker}/"

    async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.text
            elif resp.status_code == 403:
                log.warning("screener_auth_failed",
                            ticker=ticker,
                            msg="Session cookie invalid or expired")
                return None
            else:
                log.debug("screener_page_failed",
                          ticker=ticker, status=resp.status_code)
                return None
        except Exception as e:
            log.debug("screener_request_failed",
                      ticker=ticker, error=str(e)[:80])
            return None


def _find_section_table(html: str, section_name: str) -> Optional[str]:
    """
    Find the data-table HTML for a specific section (e.g. 'Quarterly Results',
    'Profit & Loss', 'Balance Sheet', 'Cash Flows', 'Ratios').
    """
    # Screener.in uses section ids like 'quarters', 'profit-loss', 'balance-sheet'
    section_patterns = {
        "quarters": r'id="quarters".*?(<table[^>]*class="data-table[^"]*".*?</table>)',
        "profit_loss": r'id="profit-loss".*?(<table[^>]*class="data-table[^"]*".*?</table>)',
        "balance_sheet": r'id="balance-sheet".*?(<table[^>]*class="data-table[^"]*".*?</table>)',
        "cash_flow": r'id="cash-flow".*?(<table[^>]*class="data-table[^"]*".*?</table>)',
        "ratios": r'id="ratios".*?(<table[^>]*class="data-table[^"]*".*?</table>)',
        "shareholding": r'id="shareholding".*?(<table[^>]*class="data-table[^"]*".*?</table>)',
    }

    pattern = section_patterns.get(section_name)
    if not pattern:
        return None

    match = re.search(pattern, html, re.DOTALL)
    if match:
        return match.group(1)

    # Fallback: search by section heading text
    heading_patterns = {
        "quarters": "Quarterly Results",
        "profit_loss": "Profit &amp; Loss",
        "balance_sheet": "Balance Sheet",
        "cash_flow": "Cash Flows",
        "ratios": "Ratios",
        "shareholding": "Shareholding Pattern",
    }
    heading = heading_patterns.get(section_name, "")
    if heading:
        fallback = re.search(
            heading + r'.*?(<table[^>]*class="data-table[^"]*".*?</table>)',
            html, re.DOTALL
        )
        if fallback:
            return fallback.group(1)

    return None


def _compute_net_worth(bs_data: dict, col_idx: int) -> Optional[float]:
    """Compute net worth from Equity Capital + Reserves if direct field not available."""
    def _get(row_name):
        vals = bs_data["rows"].get(row_name, [])
        adj = col_idx - 1
        if 0 <= adj < len(vals):
            return safe_float(vals[adj])
        return None

    equity = _get("Equity Capital") or 0
    reserves = _get("Reserves") or 0
    if equity or reserves:
        return equity + reserves
    return None


def extract_annual_records(html: str, ticker: str, isin: str) -> list:
    """Extract annual P&L + Balance Sheet + Ratios records from HTML."""
    records = []
    try:
        pl_table = _find_section_table(html, "profit_loss")
        bs_table = _find_section_table(html, "balance_sheet")
        ratios_table = _find_section_table(html, "ratios")

        if not pl_table:
            return records

        pl_data = _parse_html_table(pl_table)
        bs_data = _parse_html_table(bs_table) if bs_table else {"headers": [], "rows": {}}
        ratios_data = _parse_html_table(ratios_table) if ratios_table else {"headers": [], "rows": {}}

        # Headers are period labels like ['', 'Mar 2015', ..., 'Mar 2024', 'TTM']
        headers = pl_data["headers"]

        for i, header in enumerate(headers):
            if i == 0:  # Skip the label column header
                continue
            period_end = parse_screener_date(header)
            if not period_end:
                continue

            def _get_val(data, row_name, idx):
                """Get value from parsed table data at column index."""
                vals = data["rows"].get(row_name, [])
                adj_idx = idx - 1  # headers include label column, rows don't
                if 0 <= adj_idx < len(vals):
                    return safe_float(vals[adj_idx])
                return None

            records.append({
                "isin": isin,
                "ticker": ticker,
                "period_type": "annual",
                "period_end": period_end,
                # P&L
                "revenue_cr": _get_val(pl_data, "Sales", i) or _get_val(pl_data, "Revenue", i),
                "ebitda_cr": _get_val(pl_data, "Operating Profit", i),
                "ebitda_margin_pct": _get_val(pl_data, "OPM %", i),
                "pat_cr": _get_val(pl_data, "Net Profit", i),
                "eps": _get_val(pl_data, "EPS in Rs", i),
                # Balance sheet
                "total_debt_cr": _get_val(bs_data, "Borrowings", i),
                "cash_cr": _get_val(bs_data, "Cash Equivalents", i),
                "net_worth_cr": (
                    _get_val(bs_data, "Total Shareholders Funds", i)
                    or _compute_net_worth(bs_data, i)
                ),
                "total_assets_cr": _get_val(bs_data, "Total Assets", i),
                # Ratios
                "debtor_days": _get_val(ratios_data, "Debtor Days", i),
                "inventory_days": _get_val(ratios_data, "Inventory Days", i),
                "creditor_days": _get_val(ratios_data, "Days Payable", i),
                "roce": _get_val(ratios_data, "ROCE %", i),
                "roe": _get_val(ratios_data, "ROE %", i),
                "source": "screener_in_annual",
            })
    except Exception as e:
        log.error("extract_annual_failed", ticker=ticker, error=str(e)[:120])

    return records


def extract_quarterly_records(html: str, ticker: str, isin: str) -> list:
    """Extract last 8 quarters from HTML quarterly results table."""
    records = []
    try:
        q_table = _find_section_table(html, "quarters")
        if not q_table:
            return records

        q_data = _parse_html_table(q_table)
        headers = q_data["headers"]

        # Take last 8 periods (excluding header label column)
        period_indices = list(range(1, len(headers)))[-8:]

        for i in period_indices:
            if i >= len(headers):
                continue
            period_end = parse_screener_date(headers[i])
            if not period_end:
                continue

            def _get_val(row_name, idx):
                vals = q_data["rows"].get(row_name, [])
                adj_idx = idx - 1
                if 0 <= adj_idx < len(vals):
                    return safe_float(vals[adj_idx])
                return None

            records.append({
                "isin": isin,
                "ticker": ticker,
                "period_type": "quarterly",
                "period_end": period_end,
                "revenue_cr": _get_val("Sales", i) or _get_val("Revenue", i),
                "ebitda_cr": _get_val("Operating Profit", i),
                "ebitda_margin_pct": _get_val("OPM %", i),
                "pat_cr": _get_val("Net Profit", i),
                "source": "screener_in_quarterly",
            })
    except Exception as e:
        log.debug("extract_quarterly_failed", ticker=ticker, error=str(e)[:80])

    return records


def _screener_date_to_quarter(date_str: str) -> Optional[str]:
    """
    Convert Screener.in date header like 'Dec 2025' to Indian FY quarter string 'Q3FY26'.
    Apr-Jun = Q1, Jul-Sep = Q2, Oct-Dec = Q3, Jan-Mar = Q4.
    """
    if not date_str or date_str.strip() == "TTM":
        return None
    try:
        parts = date_str.strip().split()
        if len(parts) != 2:
            return None
        month_abbr = parts[0][:3]
        year = int(parts[1])
        month_num = int(MONTH_MAP.get(month_abbr, "0"))
        if month_num == 0:
            return None

        # Map month to Indian FY quarter
        if 4 <= month_num <= 6:
            q_num, fy_year = 1, year + 1
        elif 7 <= month_num <= 9:
            q_num, fy_year = 2, year + 1
        elif 10 <= month_num <= 12:
            q_num, fy_year = 3, year + 1
        else:
            q_num, fy_year = 4, year

        fy_suffix = str(fy_year % 100).zfill(2)
        return "Q{}FY{}".format(q_num, fy_suffix)
    except (ValueError, IndexError):
        return None


# Row name variants on Screener.in shareholding table (with &nbsp; stripped)
_SHAREHOLDING_ROW_MAP = {
    "promoters": "promoter_pct",
    "promoter": "promoter_pct",
    "promoters & promoter group": "promoter_pct",
    "fiis": "fii_pct",
    "fii": "fii_pct",
    "foreign institutional investors": "fii_pct",
    "diis": "dii_pct",
    "dii": "dii_pct",
    "domestic institutional investors": "dii_pct",
    "public": "public_pct",
    "government": "govt_pct",
    "others": "others_pct",
}


def extract_shareholding_records(html: str, ticker: str, isin: str) -> list:
    """
    Extract quarterly shareholding pattern from Screener.in HTML.
    Returns list of dicts ready for india_shareholding_patterns upsert.
    Rows: Promoters, FIIs, DIIs, Government/Others, Public — with % values.
    """
    records = []
    try:
        sh_table = _find_section_table(html, "shareholding")
        if not sh_table:
            return records

        sh_data = _parse_html_table(sh_table)
        headers = sh_data["headers"]

        if len(headers) < 2:
            return records

        # Process each quarter column (skip the label column at index 0)
        for col_idx in range(1, len(headers)):
            header_str = headers[col_idx]
            quarter = _screener_date_to_quarter(header_str)
            if not quarter:
                continue

            # Extract percentages for this column
            col_values = {}
            for row_name, values in sh_data["rows"].items():
                # Clean &nbsp; and whitespace from row name
                clean_name = row_name.replace("\xa0", "").replace("&nbsp;", "").strip().lower()
                field = _SHAREHOLDING_ROW_MAP.get(clean_name)
                if not field:
                    continue

                adj_idx = col_idx - 1
                if 0 <= adj_idx < len(values):
                    col_values[field] = safe_float(values[adj_idx])

            promoter_pct = col_values.get("promoter_pct") or 0.0
            fii_pct = col_values.get("fii_pct") or 0.0
            dii_pct = col_values.get("dii_pct") or 0.0
            public_pct = col_values.get("public_pct") or 0.0

            # Skip columns with no meaningful data
            if promoter_pct < 0.01 and fii_pct < 0.01 and public_pct < 0.01:
                continue

            records.append({
                "isin": isin,
                "ticker": ticker,
                "quarter": quarter,
                "promoter_pct": round(promoter_pct, 2),
                "fii_pct": round(fii_pct, 2),
                "dii_pct": round(dii_pct, 2),
                "mf_pct": 0.0,  # Screener.in doesn't break out MF from DII
                "insurance_pct": 0.0,
                "public_pct": round(public_pct, 2),
                "notable_holders": "[]",
            })

        # Compute QoQ deltas (records are in chronological order)
        for i in range(len(records)):
            if i == 0:
                records[i]["promoter_delta"] = 0.0
                records[i]["fii_delta"] = 0.0
                records[i]["mf_delta"] = 0.0
                records[i]["dii_delta"] = 0.0
            else:
                prev = records[i - 1]
                records[i]["promoter_delta"] = round(records[i]["promoter_pct"] - prev["promoter_pct"], 2)
                records[i]["fii_delta"] = round(records[i]["fii_pct"] - prev["fii_pct"], 2)
                records[i]["mf_delta"] = 0.0
                records[i]["dii_delta"] = round(records[i]["dii_pct"] - prev["dii_pct"], 2)

    except Exception as e:
        log.debug("extract_shareholding_failed", ticker=ticker, error=str(e)[:80])

    return records


async def fetch_and_store_shareholding(
    db,
    ticker: str,
    isin: str,
    session_cookie: str,
    html: Optional[str] = None,
) -> int:
    """
    Fetch shareholding pattern for one company from Screener.in.
    If html is already fetched (e.g. during financials batch), pass it in.
    Returns count of records stored.
    """
    if not html:
        html = await fetch_company_html(ticker, session_cookie)
    if not html:
        return 0

    records = extract_shareholding_records(html, ticker, isin)
    if not records:
        return 0

    stored = 0
    for record in records:
        try:
            from datetime import datetime as _dt
            await db.table("india_shareholding_patterns").upsert(
                {**record, "fetched_at": _dt.now().isoformat()},
                on_conflict="isin,quarter"
            ).execute()
            stored += 1
        except Exception as e:
            log.debug("store_shareholding_failed",
                      ticker=ticker, quarter=record.get("quarter"), error=str(e)[:80])

    return stored


async def fetch_and_store_financials(
    db,
    ticker: str,
    isin: str,
    session_cookie: str
) -> bool:
    """Fetch and store financials for one company. Returns True on success."""
    if not session_cookie:
        log.warning("no_screener_cookie", msg="Skipping screener fetch — no session cookie")
        return False

    html = await fetch_company_html(ticker, session_cookie)
    if not html:
        return False

    annual = extract_annual_records(html, ticker, isin)
    quarterly = extract_quarterly_records(html, ticker, isin)
    all_records = annual + quarterly

    if not all_records:
        log.debug("no_financial_records", ticker=ticker)
        return False

    stored = 0
    for record in all_records:
        try:
            await db.table("india_financials_history").upsert(
                record,
                on_conflict="isin,period_type,period_end"
            ).execute()
            stored += 1
        except Exception as e:
            log.debug("store_financial_failed",
                      ticker=ticker, error=str(e)[:80])

    log.debug("financials_stored", ticker=ticker, records=stored)
    return stored > 0


async def batch_fetch_financials(
    db,
    companies: list,
    session_cookie: str,
    delay_seconds: float = 1.2
) -> dict:
    """
    Fetch financials for a batch of companies.
    1.2s delay between requests respects Screener.in rate limits.
    """
    results = {"processed": 0, "success": 0, "failed": 0, "no_cookie": 0}

    if not session_cookie:
        log.warning("batch_fetch_skipped", reason="No screener session cookie")
        results["no_cookie"] = len(companies)
        return results

    consecutive_403s = 0

    for company in companies:
        results["processed"] += 1
        ticker = company.get("ticker", "")
        isin = company.get("isin", "")

        if not ticker:
            results["failed"] += 1
            continue

        # Detect 403 cookie expiration via fetch_company_html return
        html = await fetch_company_html(ticker, session_cookie)

        if html is None:
            # Check if this was a 403 (cookie expired)
            # fetch_company_html returns None for 403s and other failures
            consecutive_403s += 1
            results["failed"] += 1

            if consecutive_403s >= 3:
                log.error("screener_cookie_expired",
                          msg="3 consecutive failures — session cookie likely expired. Aborting batch.",
                          tip="Refresh SCREENER_SESSION_COOKIE in .env",
                          processed=results["processed"],
                          success=results["success"])
                break
        else:
            consecutive_403s = 0
            # Parse and store the HTML we already fetched
            annual = extract_annual_records(html, ticker, isin)
            quarterly = extract_quarterly_records(html, ticker, isin)
            all_records = annual + quarterly

            if all_records:
                stored = 0
                for record in all_records:
                    try:
                        await db.table("india_financials_history").upsert(
                            record,
                            on_conflict="isin,period_type,period_end"
                        ).execute()
                        stored += 1
                    except Exception as e:
                        log.debug("store_financial_failed",
                                  ticker=ticker, error=str(e)[:80])

                if stored > 0:
                    results["success"] += 1
                else:
                    results["failed"] += 1
            else:
                results["failed"] += 1

        await asyncio.sleep(delay_seconds)

        if results["processed"] % 50 == 0:
            log.info("screener_batch_progress",
                     processed=results["processed"],
                     total=len(companies),
                     success=results["success"])

    log.info("screener_batch_complete", **results)
    return results


async def batch_fetch_shareholding(
    db,
    companies: list,
    session_cookie: str,
    delay_seconds: float = 1.2,
) -> dict:
    """
    Fetch shareholding patterns for a batch of companies from Screener.in.
    Separate from financials batch to allow independent pipeline steps.
    """
    results = {"processed": 0, "success": 0, "failed": 0, "records_stored": 0}

    if not session_cookie:
        log.warning("shareholding_batch_skipped", reason="No screener session cookie")
        return results

    consecutive_403s = 0

    for company in companies:
        results["processed"] += 1
        ticker = company.get("ticker", "")
        isin = company.get("isin", "")

        if not ticker or not isin:
            results["failed"] += 1
            continue

        html = await fetch_company_html(ticker, session_cookie)

        if html is None:
            consecutive_403s += 1
            results["failed"] += 1

            if consecutive_403s >= 3:
                log.error("screener_cookie_expired_shareholding",
                          msg="3 consecutive failures — aborting shareholding batch.",
                          processed=results["processed"],
                          success=results["success"])
                break
        else:
            consecutive_403s = 0
            stored = await fetch_and_store_shareholding(
                db, ticker, isin, session_cookie, html=html
            )
            if stored > 0:
                results["success"] += 1
                results["records_stored"] += stored
            else:
                results["failed"] += 1

        await asyncio.sleep(delay_seconds)

        if results["processed"] % 50 == 0:
            log.info("shareholding_batch_progress",
                     processed=results["processed"],
                     total=len(companies),
                     success=results["success"],
                     records=results["records_stored"])

    log.info("shareholding_batch_complete", **results)
    return results
