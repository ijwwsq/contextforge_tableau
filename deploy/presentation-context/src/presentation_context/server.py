"""Presentation-context MCP server and read-only REST endpoint."""
from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .store import GuideStore, default_store

mcp = FastMCP(
    "presentation-context",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _store() -> GuideStore:
    return mcp._guide_store  # type: ignore[attr-defined]


@mcp.tool()
async def get_presentation_guidelines() -> dict[str, Any]:
    """Get the mandatory corporate guide before creating or revising a presentation.

    ALWAYS call this tool before drafting slides, an outline, speaker notes, charts,
    or visual instructions for a presentation. Treat every returned rule as binding:
    write in the voice of a serious analyst, use the specified green and gold palette,
    do not invent facts, and do not add a logo until an approved asset is supplied.
    """
    return _store().get()


async def _guidelines(_request: Any) -> JSONResponse:
    return JSONResponse(_store().get())


async def _health(_request: Any) -> JSONResponse:
    return JSONResponse({"status": "ok", "guidelines_loaded": bool(_store().get())})


@contextlib.asynccontextmanager
async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
    mcp._guide_store = default_store()  # type: ignore[attr-defined]
    async with mcp.session_manager.run():
        yield


def build_app() -> Starlette:
    app = mcp.streamable_http_app()
    app.router.lifespan_context = _lifespan
    app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))
    app.router.routes.insert(0, Route("/guidelines", _guidelines, methods=["GET"]))
    return app


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("HTTP_PORT", "8000")))


if __name__ == "__main__":
    main()
