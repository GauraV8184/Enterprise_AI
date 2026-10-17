"""Aggregate router for the versioned API prefix.

Feature routers (auth, documents, research, analytics, reports) are mounted here
as each phase implements them, so ``main.py`` stays stable.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import chat, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(chat.router)
