"""Schemas for health and service metadata endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness/readiness payload returned by the health endpoints."""

    status: Literal["ok"] = "ok"
    service: str = Field(..., description="Human-readable service name.")
    version: str = Field(..., description="Running application version.")
    environment: str = Field(..., description="Deployment environment (development/test/...).")


class ServiceInfo(BaseModel):
    """Root endpoint payload: basic service metadata."""

    name: str
    version: str
    environment: str
    docs_url: str = Field(..., description="URL of the interactive API documentation.")
