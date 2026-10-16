"""Shared FastAPI dependencies.

Settings are attached to ``app.state.settings`` in ``create_app`` and read back
here. This keeps a single source of truth per app instance and lets tests build
apps with their own settings without touching global state.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings


def get_app_settings(request: Request) -> Settings:
    """Return the ``Settings`` bound to the current application instance."""
    return request.app.state.settings


# Reusable annotated dependency. Using ``Annotated`` (the FastAPI-recommended
# style) keeps ``Depends`` out of function defaults, which satisfies lint rules.
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
