"""Application entrypoint: lifespan wiring, routers, static files.

The lifespan hook is where the background machinery starts and, importantly,
stops cleanly: the event consumer flushes whatever is still queued on shutdown
so a restart doesn't silently lose the last few seconds of behaviour.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.db import init_db, session_scope
from app.services import events as event_service

logging.basicConfig(
    level=logging.INFO if not settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("bazario")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await event_service.start_consumer()

    # Repair any SQL/vector drift left by an unclean shutdown before serving.
    try:
        from app.services import catalog

        with session_scope() as db:
            report = await catalog.reconcile(db)
        if report["repaired"] or report["orphans_deleted"]:
            logger.info("Startup reconcile: %s", report)
    except Exception:
        logger.warning("Startup reconcile failed; continuing", exc_info=True)

    scheduler = None
    if settings.scheduler_enabled:
        try:
            from app.scheduler import start_scheduler

            scheduler = start_scheduler()
        except Exception:
            logger.warning("Scheduler failed to start; continuing", exc_info=True)
    # Published on app.state so /admin/scheduler can report the live job list
    # rather than restating the configuration back to itself.
    app.state.scheduler = scheduler

    logger.info("%s ready (debug=%s)", settings.app_name, settings.debug)
    try:
        yield
    finally:
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                logger.warning("Scheduler shutdown error", exc_info=True)
        await event_service.stop_consumer()
        try:
            from app.ai.gemini_client import gemini_client

            await gemini_client.aclose()
        except Exception:
            pass


app = FastAPI(title=settings.app_name, lifespan=lifespan, debug=settings.debug)

# The React storefront runs on its own dev origin and authenticates with the
# same signed session cookie the Jinja pages use, so it must send credentials -
# which means an explicit origin list, never `["*"]`. Browsers reject a wildcard
# `Access-Control-Allow-Origin` on a credentialed request outright.
#
# localhost:5173 and localhost:8000 are *same-site* for cookie purposes (the port
# is not part of a site), so `samesite="lax"` keeps working in dev. Serving the
# storefront from a different registrable domain in production would need
# `samesite="none"; secure` and real HTTPS - see NOTES.md.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
)

app.mount("/static", StaticFiles(directory=str(settings.resolve("app/static"))), name="static")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """`current_user` raises a 303 with a Location header to bounce anonymous
    visitors to the login page. Honour that for page requests.

    JSON endpoints depend on `api_user`, which raises a plain 401 and never
    carries a Location, so they don't rely on this. The `/api/` guard stays as a
    backstop: if anything under the API ever does raise a redirecting exception,
    turning it into a redirect would hand a JSON client an HTML login page.
    """
    location = (exc.headers or {}).get("Location")
    if location and not request.url.path.startswith("/api/"):
        return RedirectResponse(location, status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


from app.routers import admin, auth, events, pages, recs, storefront  # noqa: E402

app.include_router(events.router)
app.include_router(auth.router)
app.include_router(auth.api_router)
app.include_router(recs.router)
app.include_router(storefront.router)
app.include_router(admin.router)
app.include_router(pages.router)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {
        "status": "ok",
        "events": event_service.stats(),
        "gemini_configured": settings.gemini_configured,
    }
