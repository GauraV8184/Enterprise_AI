"""Health and service-metadata endpoints.

``/health`` is a liveness probe (the process is up). Keep it dependency-free so
orchestrators can restart a wedged process quickly. Deeper readiness checks
(database, vector store, LLM provider) are added in the phases that introduce
those dependencies.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import SettingsDep
from app.schemas.health import HealthResponse, ServiceInfo

router = APIRouter(tags=["system"])


@router.get("/", response_model=ServiceInfo, summary="Service metadata")
def read_root(settings: SettingsDep) -> ServiceInfo:
    return ServiceInfo(
        name=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        docs_url="/docs",
    )


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
    )
