import re
from typing import Optional

import supabase._async.client as _async_mod
import supabase._sync.client as _sync_mod
from supabase import Client
from supabase._async.client import AsyncClient

from india_alpha.config import get_settings

# Patch: Supabase SDK validates keys as JWTs but newer projects use sb_secret_/sb_publishable_ format.
# Monkey-patch to accept both formats.
_orig_sync_init = _sync_mod.SyncClient.__init__
_orig_async_init = _async_mod.AsyncClient.__init__

def _patched_sync_init(self, supabase_url, supabase_key, options=None):
    # Temporarily replace the re.match to accept new key formats
    _real_match = re.match
    def _permissive_match(pattern, string, *args, **kwargs):
        if "A-Za-z0-9-_=" in pattern and (
            string.startswith("sb_") or string.startswith("eyJ")
        ):
            return True
        return _real_match(pattern, string, *args, **kwargs)
    re.match = _permissive_match
    try:
        _orig_sync_init(self, supabase_url, supabase_key, options)
    finally:
        re.match = _real_match

def _patched_async_init(self, supabase_url, supabase_key, options=None):
    _real_match = re.match
    def _permissive_match(pattern, string, *args, **kwargs):
        if "A-Za-z0-9-_=" in pattern and (
            string.startswith("sb_") or string.startswith("eyJ")
        ):
            return True
        return _real_match(pattern, string, *args, **kwargs)
    re.match = _permissive_match
    try:
        _orig_async_init(self, supabase_url, supabase_key, options)
    finally:
        re.match = _real_match

_sync_mod.SyncClient.__init__ = _patched_sync_init
_async_mod.AsyncClient.__init__ = _patched_async_init

# --- Client accessors ---

_sync_client: Optional[Client] = None
_async_client: Optional[AsyncClient] = None

def get_db() -> Client:
    """Synchronous Supabase client for simple queries."""
    global _sync_client
    if _sync_client is None:
        settings = get_settings()
        if not settings.supabase_url or not settings.supabase_key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
        from supabase import create_client
        _sync_client = create_client(settings.supabase_url, settings.supabase_key)
    return _sync_client

async def get_async_db() -> AsyncClient:
    """Async Supabase client for pipeline operations."""
    global _async_client
    if _async_client is None:
        settings = get_settings()
        if not settings.supabase_url or not settings.supabase_key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
        from supabase._async.client import create_client as create_async_client
        _async_client = await create_async_client(settings.supabase_url, settings.supabase_key)
    return _async_client


async def fetch_all_rows(db, table: str, select: str = "*", page_size: int = 1000, **filters) -> list:
    """
    Paginated fetch that bypasses Supabase PostgREST's 1000-row max limit.
    Uses .range() to fetch all rows in pages.

    Usage:
        rows = await fetch_all_rows(db, "india_companies", select="ticker, isin")
        rows = await fetch_all_rows(db, "india_financials_history", select="isin, ticker", eq={"period_type": "annual"})
    """
    all_rows = []
    offset = 0
    while True:
        query = db.table(table).select(select).range(offset, offset + page_size - 1)
        # Apply equality filters
        for key, val in filters.items():
            if key == "eq":
                for col, v in val.items():
                    query = query.eq(col, v)
            elif key == "gte":
                for col, v in val.items():
                    query = query.gte(col, v)
        result = await query.execute()
        rows = result.data or []
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    return all_rows
