"""FastAPI main application with CORS, authentication, and error handling"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

logger = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from openrag.api.deps import get_db
from openrag.api.files_api import router as files_router
from openrag.api.workspace_file_api import router as workspace_files_router
from openrag.api.embed_preview_api import router as embed_preview_router
from openrag.api.search_api import router as search_router
from openrag.api.users_api import router as users_router
from openrag.api.teams_api import router as teams_router
from openrag.api.share_api import router as share_router
from openrag.api.workspaces_api import router as workspaces_router
from openrag.api.tasks_api import router as tasks_router
from openrag.api.broker_api import router as broker_router
from openrag.api.roles_api import router as roles_router
from openrag.api.service_api import router as service_router
from openrag.api.service_tokens_admin import router as service_tokens_admin_router
from openrag.api.permissions_api import router as file_permissions_router
from openrag.api.permissions_api import user_permissions_router
from openrag.api.traces_api import router as traces_router
from openrag.api.eval_api import router as eval_router
from openrag.config import get_config
from openrag.tracing.context import reset_trace_context, set_trace_context

TRACE_HEADER = "X-OpenRag-Trace-Id"

# Create FastAPI application
app = FastAPI(
    title="OpenRag API",
    description="Enterprise RAG system with permission management",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# CORS Configuration
config = get_config()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",  # React dev server
        "http://localhost:3001",  # React dev server (alt port)
        "http://localhost:5173",  # Vite dev server
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def trace_context_middleware(request: Request, call_next):
    """Attach a per-request trace context and echo the trace id."""

    trace_id = request.headers.get(TRACE_HEADER) or uuid.uuid4().hex
    trace_type = "retrieval" if request.url.path.startswith("/search") else None
    set_trace_context(
        trace_id=trace_id,
        trace_type=trace_type,
        sampling_reason="api_request",
    )
    try:
        response = await call_next(request)
        response.headers[TRACE_HEADER] = trace_id
        return response
    finally:
        reset_trace_context()


# Exception Handlers
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """
    Handle HTTP exceptions with consistent format.

    Args:
        request: FastAPI request
        exc: HTTP exception

    Returns:
        JSON response with error details
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    Handle request validation errors with detailed information.

    Args:
        request: FastAPI request
        exc: Validation error

    Returns:
        JSON response with validation error details
    """
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors()},
    )


@app.exception_handler(SQLAlchemyError)
async def database_exception_handler(
    request: Request, exc: SQLAlchemyError
) -> JSONResponse:
    """
    Handle database errors without exposing internal details.

    Args:
        request: FastAPI request
        exc: SQLAlchemy error

    Returns:
        JSON response with generic error message
    """
    logger.exception("SQLAlchemy error on %s", getattr(request, "url", None))

    detail: Any = "Database error occurred"
    if os.getenv("DEBUG", "").lower() in ("true", "1", "yes"):
        detail = str(getattr(exc, "orig", exc))[:800]

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": detail},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Handle unexpected errors without exposing internal details.

    Args:
        request: FastAPI request
        exc: Exception

    Returns:
        JSON response with generic error message
    """
    # Log the actual error for debugging (in production, use proper logging)
    # logger.error(f"Unexpected error: {str(exc)}")

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


# Health Check Endpoint
@app.get("/health", tags=["health"])
async def health_check() -> Dict[str, Any]:
    """
    Health check endpoint to verify API and database status.

    Returns:
        Health status information
    """
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Check database connectivity
    try:
        db_generator = get_db()
        db = next(db_generator)
        # Simple query to verify connection
        db.execute(text("SELECT 1"))
        health_status["database"] = "connected"
        # Cleanup
        try:
            next(db_generator)
        except StopIteration:
            pass
    except Exception as e:
        health_status["database"] = "disconnected"
        health_status["status"] = "unhealthy"
        # Don't expose error details in production
        # health_status["error"] = str(e)

    return health_status


# Include Routers
app.include_router(users_router)
app.include_router(teams_router)
app.include_router(workspaces_router)
app.include_router(tasks_router)
app.include_router(files_router)
app.include_router(workspace_files_router)
app.include_router(embed_preview_router)
app.include_router(share_router)
app.include_router(search_router)
app.include_router(broker_router)
app.include_router(service_router)
app.include_router(service_tokens_admin_router)
app.include_router(roles_router)
app.include_router(file_permissions_router)
app.include_router(user_permissions_router)
app.include_router(traces_router)
app.include_router(eval_router)


# Startup Event
@app.on_event("startup")
async def startup_event():
    """
    Application startup event handler.

    Performs initialization tasks like:
    - Verifying database connection
    - Loading configuration
    - Initializing services
    - Starting background scheduler
    """
    # Verify database connection and ensure tables exist
    try:
        import openrag.models  # noqa: F401 — ensure all models registered with Base.metadata
        from openrag.database import init_db

        init_db()
        db_generator = get_db()
        db = next(db_generator)
        db.execute(text("SELECT 1"))
        print("✓ Database connection verified, tables synced")
        try:
            next(db_generator)
        except StopIteration:
            pass
    except Exception as e:
        print(f"✗ Database connection failed: {e}")

    # Start background scheduler for timeout recovery
    try:
        from openrag.scheduler import start_scheduler

        start_scheduler()
        print("✓ Background scheduler started")
    except Exception as e:
        print(f"✗ Failed to start scheduler: {e}")

    print(f"✓ OpenRag API started - {app.title} v{app.version}")


# Shutdown Event
@app.on_event("shutdown")
async def shutdown_event():
    """
    Application shutdown event handler.

    Performs cleanup tasks like:
    - Closing database connections
    - Cleaning up resources
    - Stopping background scheduler
    """
    # Stop background scheduler
    try:
        from openrag.scheduler import stop_scheduler

        stop_scheduler()
        print("✓ Background scheduler stopped")
    except Exception as e:
        print(f"✗ Failed to stop scheduler: {e}")

    print("✓ OpenRag API shutting down")
