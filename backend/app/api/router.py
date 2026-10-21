"""Aggregate router for the versioned API prefix.

Feature routers (auth, documents, research, analytics, reports) are mounted here
as each phase implements them, so ``main.py`` stays stable.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import chat, documents, health, reports, workflow

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(chat.router)
api_router.include_router(documents.router)
api_router.include_router(workflow.router)
api_router.include_router(reports.router)
