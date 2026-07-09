"""Dashboard-context MCP server.

Exposes tools that give an LLM a compact, structured description of a
Tableau dashboard: Tableau metadata + business context from the YAML
catalog, merged in a stable shape.

Transport: Streamable HTTP on /mcp (default port 8000).
Plus a /health endpoint the container's healthcheck hits.
"""
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

from .catalog import Catalog, default_catalog
from .tableau import TableauClient, TableauConfig

log = logging.getLogger("dashboard_context")


# DNS rebinding protection guards browsers making cross-origin fetches. This
# server sits on an internal Docker network behind the gateway — no browser
# ever hits it directly — so disable it and let any Host through.
mcp = FastMCP(
    "dashboard-context",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _catalog() -> Catalog:
    return mcp._catalog  # type: ignore[attr-defined]


def _client() -> TableauClient | None:
    return mcp._tableau  # type: ignore[attr-defined]


def _has_tableau_creds() -> bool:
    return all(os.environ.get(k) for k in ("TABLEAU_SERVER", "TABLEAU_PAT_NAME", "TABLEAU_PAT_VALUE"))


@mcp.tool()
async def describe_dashboard(dashboard_id: str) -> dict[str, Any]:
    """Get the authoritative description of a Tableau dashboard.

    ALWAYS call this before answering any question about a specific Tableau
    dashboard, workbook, or view — including "what does X show", "how is X
    calculated", "who owns X", "why is this number what it is", or "should I
    trust X". Do not answer from prior knowledge. The operator maintains a
    hand-written catalog with definitions, calculation logic, data sources,
    caveats, and glossary that override anything you might infer from the
    name alone.

    The response merges live Tableau metadata (title, project, workbook,
    updated_at) with hand-written business context — `description` (what is
    in the dashboard), `how_built` (data sources, joins, calc fields,
    caveats), `purpose`, `owner`, `kpis`, `freshness_sla`, `glossary`, and
    `notes`.

    If `sources.catalog` is false, the operator has not documented this
    dashboard — say so explicitly rather than guessing.

    Args:
        dashboard_id: Tableau view LUID (preferred) or catalog slug.
    """
    catalog_entry = _catalog().lookup(dashboard_id)

    tableau_meta: dict[str, Any] | None = None
    client = _client()
    luid = (catalog_entry or {}).get("luid") or dashboard_id
    if client is not None:
        try:
            view = await client.view(luid)
            if view is not None:
                workbook_ref = view.get("workbook") or {}
                workbook = await client.workbook(workbook_ref.get("id")) if workbook_ref.get("id") else None
                tableau_meta = {
                    "name": view.get("name"),
                    "content_url": view.get("contentUrl"),
                    "project": (view.get("project") or {}).get("name"),
                    "owner": (view.get("owner") or {}).get("id"),
                    "created_at": view.get("createdAt"),
                    "updated_at": view.get("updatedAt"),
                    "workbook": {
                        "id": workbook_ref.get("id"),
                        "name": (workbook or {}).get("name"),
                        "description": (workbook or {}).get("description"),
                    } if workbook_ref.get("id") else None,
                }
        except Exception as exc:  # pragma: no cover - surfaced to the caller
            log.warning("Tableau metadata fetch failed for %s: %s", luid, exc)
            tableau_meta = {"error": str(exc)}

    return {
        "id": dashboard_id,
        "resolved_luid": luid,
        "tableau": tableau_meta,
        "business_context": catalog_entry,
        "sources": {
            "tableau": tableau_meta is not None,
            "catalog": catalog_entry is not None,
        },
    }


@mcp.tool()
async def list_dashboards() -> list[dict[str, Any]]:
    """List every dashboard registered in the operator's catalog.

    Call this when the user mentions a dashboard by a fuzzy name and you need
    to resolve it to a specific slug or LUID before calling `describe_dashboard`.
    """
    return [
        {
            "id": entry.get("slug") or entry.get("luid"),
            "luid": entry.get("luid"),
            "slug": entry.get("slug"),
            "name": entry.get("name"),
            "owner": entry.get("owner"),
            "purpose": entry.get("purpose"),
        }
        for entry in _catalog().all()
    ]


@contextlib.asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    mcp._catalog = default_catalog()  # type: ignore[attr-defined]
    mcp._tableau = None  # type: ignore[attr-defined]
    if _has_tableau_creds():
        try:
            mcp._tableau = TableauClient(TableauConfig.from_env())  # type: ignore[attr-defined]
        except KeyError as exc:
            log.warning("Tableau creds incomplete (%s) — running in catalog-only mode.", exc)
    else:
        log.info("Tableau creds missing — running in catalog-only mode.")
    # FastMCP's streamable_http_app relies on a task group owned by its
    # session_manager; when we mount it inside our own Starlette we must
    # start it explicitly (its own run() is not invoked in this path).
    async with mcp.session_manager.run():
        try:
            yield
        finally:
            client = _client()
            if client is not None:
                await client.close()


async def _health(_request: Any) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "tableau_configured": _has_tableau_creds(),
            "catalog_entries": len(_catalog().all()),
        }
    )


def build_app() -> Starlette:
    # Use FastMCP's own Starlette app as the outer ASGI. Mounting it inside
    # a wrapper Starlette breaks route method matching (its /mcp route
    # exposes methods=[] which the wrapper rejects with 421).
    app = mcp.streamable_http_app()
    app.router.lifespan_context = _lifespan
    app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))
    return app


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    port = int(os.environ.get("HTTP_PORT", "8000"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
