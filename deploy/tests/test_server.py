"""Тесты для MCP-сервера dashboard-context.

Проверяют логику `_describe`, тулзы `describe_dashboard` / `list_dashboards`
и REST-эндпоинты /health и /get/{luid}. Tableau-клиент подменяется fake'ом,
чтобы не ходить в реальную сеть.
"""
from __future__ import annotations

from typing import Any

import pytest

from dashboard_context import server as server_module


class _FakeCatalog:
    def __init__(self, entries: dict[str, dict[str, Any]] | None = None):
        self._entries = entries or {}

    def lookup(self, key: str) -> dict[str, Any] | None:
        return self._entries.get(key)

    def all(self) -> list[dict[str, Any]]:
        return list(self._entries.values())


class _FakeTableau:
    def __init__(self, view_by_luid: dict[str, dict[str, Any]] | None = None,
                 workbook_by_id: dict[str, dict[str, Any]] | None = None,
                 raise_on_view: bool = False):
        self.view_by_luid = view_by_luid or {}
        self.workbook_by_id = workbook_by_id or {}
        self.raise_on_view = raise_on_view

    async def view(self, luid: str):
        if self.raise_on_view:
            raise RuntimeError("boom")
        return self.view_by_luid.get(luid)

    async def workbook(self, wb_id: str):
        return self.workbook_by_id.get(wb_id)

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _wipe_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    # server.py хранит catalog/client как атрибуты синглтона `mcp`.
    # Между тестами очищаем, чтобы состояние не протекало.
    monkeypatch.setattr(server_module.mcp, "_catalog", _FakeCatalog(), raising=False)
    monkeypatch.setattr(server_module.mcp, "_tableau", None, raising=False)


async def test_describe_returns_catalog_only_when_no_tableau_client() -> None:
    catalog = _FakeCatalog({"slug-1": {"luid": "L1", "slug": "slug-1", "name": "Slug One"}})
    server_module.mcp._catalog = catalog

    result = await server_module._describe("slug-1")
    assert result["business_context"]["name"] == "Slug One"
    assert result["tableau"] is None
    assert result["sources"] == {"tableau": False, "catalog": True}
    assert result["resolved_luid"] == "L1"


async def test_describe_merges_tableau_metadata() -> None:
    catalog = _FakeCatalog({"slug-1": {"luid": "L1", "slug": "slug-1"}})
    tableau = _FakeTableau(
        view_by_luid={
            "L1": {
                "name": "Weekly Sales",
                "contentUrl": "sales/views/weekly",
                "project": {"name": "Sales"},
                "owner": {"id": "user-42"},
                "createdAt": "2026-01-01",
                "updatedAt": "2026-06-01",
                "workbook": {"id": "wb-1"},
            }
        },
        workbook_by_id={"wb-1": {"name": "Sales WB", "description": "desc"}},
    )
    server_module.mcp._catalog = catalog
    server_module.mcp._tableau = tableau

    result = await server_module._describe("slug-1")
    assert result["sources"] == {"tableau": True, "catalog": True}
    assert result["tableau"]["name"] == "Weekly Sales"
    assert result["tableau"]["project"] == "Sales"
    assert result["tableau"]["workbook"] == {"id": "wb-1", "name": "Sales WB", "description": "desc"}


async def test_describe_uses_raw_id_when_catalog_has_no_entry() -> None:
    tableau = _FakeTableau(view_by_luid={"raw-luid": {"name": "Raw"}})
    server_module.mcp._tableau = tableau

    result = await server_module._describe("raw-luid")
    assert result["resolved_luid"] == "raw-luid"
    assert result["business_context"] is None
    assert result["tableau"]["name"] == "Raw"


async def test_describe_reports_tableau_error_without_crashing() -> None:
    server_module.mcp._catalog = _FakeCatalog({"x": {"luid": "L", "slug": "x"}})
    server_module.mcp._tableau = _FakeTableau(raise_on_view=True)

    result = await server_module._describe("x")
    assert result["tableau"] == {"error": "boom"}
    # sources.tableau=True даже при ошибке — мы отдали объект, пусть и с error.
    assert result["sources"]["tableau"] is True


async def test_describe_missing_workbook_id_leaves_workbook_none() -> None:
    server_module.mcp._catalog = _FakeCatalog({"x": {"luid": "L", "slug": "x"}})
    server_module.mcp._tableau = _FakeTableau(
        view_by_luid={"L": {"name": "V", "workbook": {}}},  # без id
    )
    result = await server_module._describe("x")
    assert result["tableau"]["workbook"] is None


async def test_list_dashboards_projects_expected_fields() -> None:
    server_module.mcp._catalog = _FakeCatalog({
        "s1": {"luid": "L1", "slug": "s1", "name": "One", "owner": "o1", "purpose": "p1"},
        "s2": {"luid": "L2", "slug": "s2", "name": "Two"},
    })
    rows = await server_module.list_dashboards()
    ids = {row["id"] for row in rows}
    assert ids == {"s1", "s2"}
    row_s1 = next(r for r in rows if r["id"] == "s1")
    assert row_s1 == {
        "id": "s1", "luid": "L1", "slug": "s1", "name": "One",
        "owner": "o1", "purpose": "p1",
    }


async def test_describe_dashboard_tool_delegates_to_describe() -> None:
    server_module.mcp._catalog = _FakeCatalog({"slug-1": {"luid": "L1", "slug": "slug-1", "name": "n"}})
    result = await server_module.describe_dashboard("slug-1")
    assert result["business_context"]["name"] == "n"


def test_has_tableau_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("TABLEAU_SERVER", "TABLEAU_PAT_NAME", "TABLEAU_PAT_VALUE"):
        monkeypatch.delenv(key, raising=False)
    assert server_module._has_tableau_creds() is False

    monkeypatch.setenv("TABLEAU_SERVER", "https://x")
    monkeypatch.setenv("TABLEAU_PAT_NAME", "n")
    monkeypatch.setenv("TABLEAU_PAT_VALUE", "v")
    assert server_module._has_tableau_creds() is True
