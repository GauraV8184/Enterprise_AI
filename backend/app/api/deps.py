"""Shared FastAPI dependencies.

Settings are attached to ``app.state.settings`` in ``create_app`` and read back
here. The chat model is built once during startup and stored on
``app.state.chat_model``; tests replace it via ``app.dependency_overrides`` or by
setting the attribute directly, so the suite never touches the network.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from langchain_core.language_models import BaseChatModel

from app.core.config import Settings
from app.llm.errors import LlmNotConfiguredError


def get_app_settings(request: Request) -> Settings:
    """Return the ``Settings`` bound to the current application instance."""
    return request.app.state.settings


def get_chat_model(request: Request) -> BaseChatModel:
    """Return the app's chat model, or fail clearly if none is configured."""
    model = getattr(request.app.state, "chat_model", None)
    if model is None:
        raise LlmNotConfiguredError()
    return model


# Reusable annotated dependencies. Using ``Annotated`` (the FastAPI-recommended
# style) keeps ``Depends`` out of function defaults, which satisfies lint rules.
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
ChatModelDep = Annotated[BaseChatModel, Depends(get_chat_model)]
