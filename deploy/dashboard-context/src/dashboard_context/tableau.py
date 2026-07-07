"""Thin Tableau REST client — only what dashboard-context needs.

Deliberately narrow: sign-in, look up a view by LUID, read fields on the
underlying workbook. All calls carry the PAT from env; per-user forwarding
lives at the gateway layer (X-Upstream-Authorization) and is a next step.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class TableauConfig:
    server: str
    site_name: str
    pat_name: str
    pat_value: str
    api_version: str = "3.22"

    @classmethod
    def from_env(cls) -> "TableauConfig":
        return cls(
            server=os.environ["TABLEAU_SERVER"].rstrip("/"),
            site_name=os.environ.get("TABLEAU_SITE_NAME", ""),
            pat_name=os.environ["TABLEAU_PAT_NAME"],
            pat_value=os.environ["TABLEAU_PAT_VALUE"],
            api_version=os.environ.get("TABLEAU_API_VERSION", "3.22"),
        )


class TableauClient:
    """Minimal REST client. Signs in lazily and caches the auth token."""

    def __init__(self, cfg: TableauConfig, timeout: float = 15.0):
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=f"{cfg.server}/api/{cfg.api_version}",
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        self._token: str | None = None
        self._site_id: str | None = None

    async def _signin(self) -> None:
        if self._token:
            return
        payload = {
            "credentials": {
                "personalAccessTokenName": self._cfg.pat_name,
                "personalAccessTokenSecret": self._cfg.pat_value,
                "site": {"contentUrl": self._cfg.site_name},
            }
        }
        r = await self._client.post("/auth/signin", json=payload)
        r.raise_for_status()
        body = r.json()["credentials"]
        self._token = body["token"]
        self._site_id = body["site"]["id"]

    async def _get(self, path: str) -> dict[str, Any]:
        await self._signin()
        r = await self._client.get(
            path,
            headers={"X-Tableau-Auth": self._token or ""},
        )
        r.raise_for_status()
        return r.json()

    async def view(self, luid: str) -> dict[str, Any] | None:
        """Fetch a view (dashboard) by LUID. Returns None on 404."""
        await self._signin()
        try:
            data = await self._get(f"/sites/{self._site_id}/views/{luid}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        return data.get("view")

    async def workbook(self, workbook_id: str) -> dict[str, Any] | None:
        try:
            data = await self._get(f"/sites/{self._site_id}/workbooks/{workbook_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        return data.get("workbook")

    async def close(self) -> None:
        await self._client.aclose()
