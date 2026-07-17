"""FastAPI application factory and infrastructure endpoints."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from nico_agent.config import Settings, get_settings
from nico_agent.database import Database
from nico_agent.domain.errors import AccessDenied, DomainError
from nico_agent.domain_api import router as domain_router
from nico_agent.growth_api import router as growth_router
from nico_agent.health import (
    HealthServiceProtocol,
    InfrastructureResources,
    ReadinessReport,
    build_health_service,
)
from nico_agent.logging import configure_logging, request_id_context

logger = logging.getLogger(__name__)
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class LivenessResponse(BaseModel):
    status: str
    service: str
    version: str


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a validated correlation ID and emit one request completion log."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if _VALID_REQUEST_ID.fullmatch(supplied) else str(uuid4())
        request.state.request_id = request_id
        token = request_id_context.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "http request completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                },
            )
            return response
        except Exception as exc:
            logger.error(
                "http request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "exception_type": type(exc).__name__,
                },
            )
            response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_context.reset(token)


def create_app(
    settings: Settings | None = None,
    health_service: HealthServiceProtocol | None = None,
    database: Database | None = None,
) -> FastAPI:
    runtime_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(runtime_settings.log_level)
        resources: InfrastructureResources | None = None
        if health_service is None:
            resources = InfrastructureResources.create(runtime_settings)
            app.state.health_service = build_health_service(runtime_settings, resources)
            app.state.database = Database(resources.engine)
        logger.info(
            "api started",
            extra={
                "environment": runtime_settings.environment,
                "version": runtime_settings.app_version,
            },
        )
        try:
            yield
        finally:
            if resources is not None:
                await resources.close()
            logger.info("api stopped")

    app = FastAPI(
        title=runtime_settings.app_name,
        version=runtime_settings.app_version,
        summary="General-purpose growing multi-agent service platform",
        lifespan=lifespan,
    )
    app.state.settings = runtime_settings
    app.state.health_service = health_service
    app.state.database = database
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Request-ID", "X-Tenant-ID", "X-Actor-ID"],
        expose_headers=["X-Request-ID"],
    )

    @app.get("/api/v1/health/live", response_model=LivenessResponse, tags=["health"])
    async def liveness() -> LivenessResponse:
        return LivenessResponse(
            status="alive",
            service=runtime_settings.app_name,
            version=runtime_settings.app_version,
        )

    @app.get(
        "/api/v1/health/ready",
        response_model=ReadinessReport,
        responses={503: {"model": ReadinessReport, "description": "A dependency is unavailable"}},
        tags=["health"],
    )
    async def readiness(request: Request) -> Any:
        service: HealthServiceProtocol | None = request.app.state.health_service
        if service is None:
            report = ReadinessReport(status="not_ready", components={})
        else:
            report = await service.readiness()
        if report.status == "not_ready":
            return JSONResponse(status_code=503, content=report.model_dump(mode="json"))
        return report

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        status_code = 400
        if exc.code == "RESOURCE_NOT_FOUND":
            status_code = 404
        elif exc.code in {"REVISION_CONFLICT", "INVALID_STATE_TRANSITION"}:
            status_code = 409
        elif isinstance(exc, AccessDenied):
            status_code = 403
        elif exc.code == "DATABASE_UNAVAILABLE":
            status_code = 503
        return JSONResponse(
            status_code=status_code,
            content={"code": exc.code, "message": exc.message, "details": exc.details},
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(_request: Request, _exc: IntegrityError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "code": "DATA_CONFLICT",
                "message": "the operation conflicts with an existing resource or reference",
                "details": {},
            },
        )

    app.include_router(domain_router)
    app.include_router(growth_router)

    return app
