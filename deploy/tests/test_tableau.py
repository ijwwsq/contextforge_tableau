"""Тесты для тонкого Tableau REST-клиента."""
from __future__ import annotations

import httpx
import pytest

from dashboard_context.tableau import TableauClient, TableauConfig


def _cfg() -> TableauConfig:
    return TableauConfig(
        server="https://tableau.example.com",
        site_name="mysite",
        pat_name="pat",
        pat_value="secret",
        api_version="3.22",
    )


def _client_with_transport(transport: httpx.MockTransport) -> TableauClient:
    c = TableauClient(_cfg())
    # Подсовываем MockTransport, но сохраняем ту же base_url/timeout, что и в реальном.
    c._client = httpx.AsyncClient(
        base_url="https://tableau.example.com/api/3.22",
        transport=transport,
        headers={"Accept": "application/json"},
    )
    return c


async def test_signin_and_view_returns_dict() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/auth/signin"):
            return httpx.Response(
                200,
                json={"credentials": {"token": "tok", "site": {"id": "site-42"}}},
            )
        if "/views/" in request.url.path:
            # X-Tableau-Auth должен уйти на защищённые эндпоинты после логина.
            assert request.headers.get("x-tableau-auth") == "tok"
            return httpx.Response(200, json={"view": {"name": "V", "id": "view-1"}})
        raise AssertionError(f"unexpected call: {request.url}")

    tab = _client_with_transport(httpx.MockTransport(handler))
    view = await tab.view("view-luid")
    assert view == {"name": "V", "id": "view-1"}
    assert tab._token == "tok"
    assert tab._site_id == "site-42"

    # Второй вызов — signin повторно не идёт, токен закэширован.
    await tab.view("view-luid")
    signin_calls = [c for c in calls if c[1].endswith("/auth/signin")]
    assert len(signin_calls) == 1

    await tab.close()


async def test_view_returns_none_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/signin"):
            return httpx.Response(200, json={"credentials": {"token": "t", "site": {"id": "s"}}})
        return httpx.Response(404)

    tab = _client_with_transport(httpx.MockTransport(handler))
    assert await tab.view("missing") is None
    await tab.close()


async def test_view_propagates_non_404_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/signin"):
            return httpx.Response(200, json={"credentials": {"token": "t", "site": {"id": "s"}}})
        return httpx.Response(500)

    tab = _client_with_transport(httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await tab.view("boom")
    await tab.close()


async def test_workbook_happy_path_and_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/signin"):
            return httpx.Response(200, json={"credentials": {"token": "t", "site": {"id": "s"}}})
        if request.url.path.endswith("/workbooks/wb-1"):
            return httpx.Response(200, json={"workbook": {"name": "WB"}})
        return httpx.Response(404)

    tab = _client_with_transport(httpx.MockTransport(handler))
    assert (await tab.workbook("wb-1")) == {"name": "WB"}
    assert (await tab.workbook("missing")) is None
    await tab.close()


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABLEAU_SERVER", "https://x.example.com/")
    monkeypatch.setenv("TABLEAU_SITE_NAME", "s")
    monkeypatch.setenv("TABLEAU_PAT_NAME", "n")
    monkeypatch.setenv("TABLEAU_PAT_VALUE", "v")
    monkeypatch.setenv("TABLEAU_API_VERSION", "3.20")

    cfg = TableauConfig.from_env()
    assert cfg.server == "https://x.example.com"  # хвостовой слэш должен быть срезан
    assert cfg.site_name == "s"
    assert cfg.pat_name == "n"
    assert cfg.pat_value == "v"
    assert cfg.api_version == "3.20"


def test_config_from_env_requires_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("TABLEAU_SERVER", "TABLEAU_PAT_NAME", "TABLEAU_PAT_VALUE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TABLEAU_SERVER", "https://x")
    monkeypatch.setenv("TABLEAU_PAT_NAME", "n")
    # PAT_VALUE намеренно не задаём.
    with pytest.raises(KeyError):
        TableauConfig.from_env()
