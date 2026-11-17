# --- Add this route to app/main.py, near the other /admin/* routes -------
# (uses the same _check_token helper already defined in main.py)

@app.get("/admin/orders-debug")
async def orders_debug(token: str | None = None, refetch: bool = True):
    """Diagnose why Order Status might be serving stale data.
    Usage: GET /admin/orders-debug?token=<ADMIN_TOKEN>
    Add &refetch=false to inspect the cache without forcing a fresh
    network call to the Sheets API.
    """
    _check_token(token)
    import asyncio
    return await asyncio.to_thread(orders.debug_status, refetch)
