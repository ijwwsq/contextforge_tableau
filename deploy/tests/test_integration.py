"""Интеграционный тест: реальный HttpCatalog против реальной админки.

Проверяет главный сценарий последнего изменения: MCP-сервер вытаскивает
контекст не из YAML напрямую, а из ручки `/api/dashboards` админ-сервиса.
Никаких моков между катагом и админкой — только внутри TestClient.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from dashboard_context import admin
from dashboard_context.catalog import HttpCatalog


@pytest.fixture
def catalog_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "dashboards.yml"
    path.write_text(
        """
dashboards:
  - luid: LUID-1
    slug: alpha
    name: Alpha
    owner: alpha-owner
    kpis:
      - one
      - two
  - luid: LUID-2
    slug: beta
    name: Beta
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("CATALOG_PATH", str(path))
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "x")
    return path


def test_http_catalog_pulls_from_running_admin(
    catalog_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_client = TestClient(admin.build_app())

    # Патчим httpx.get внутри catalog так, чтобы он шёл в наш TestClient
    # вместо реальной сети. Всё, что дальше, — настоящий Starlette-стек админки.
    def routed_get(url: str, **kwargs):
        # url приходит как "http://admin/api/dashboards", берём только путь.
        # TestClient не принимает timeout — отсекаем его из kwargs.
        kwargs.pop("timeout", None)
        path = httpx.URL(url).path
        return admin_client.get(path, **kwargs)

    monkeypatch.setattr("dashboard_context.catalog.httpx.get", routed_get)

    cat = HttpCatalog("http://admin/api/dashboards", ttl_seconds=0.0)

    # 1) lookup по luid и по slug должен работать в обоих направлениях.
    assert cat.lookup("LUID-1")["name"] == "Alpha"
    assert cat.lookup("alpha")["name"] == "Alpha"
    assert cat.lookup("beta")["luid"] == "LUID-2"
    assert cat.lookup("does-not-exist") is None

    # 2) all() отдаёт полный набор — как это увидит list_dashboards.
    names = {e["name"] for e in cat.all()}
    assert names == {"Alpha", "Beta"}

    # 3) вложенные структуры (kpis) пережили сериализацию в JSON и обратно.
    assert cat.lookup("alpha")["kpis"] == ["one", "two"]


def test_admin_write_visible_to_catalog_after_ttl(
    catalog_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_client = TestClient(admin.build_app())

    def routed_get(url: str, **kwargs):
        path = httpx.URL(url).path
        return admin_client.get(path, **kwargs)

    monkeypatch.setattr("dashboard_context.catalog.httpx.get", routed_get)

    cat = HttpCatalog("http://admin/api/dashboards", ttl_seconds=0.0)
    assert cat.lookup("alpha")["name"] == "Alpha"

    # Редактируем через админку — это отправляет POST в форму, которая
    # атомарно перезаписывает YAML на диске.
    import base64
    creds = base64.b64encode(b"admin:x").decode()
    r = admin_client.post(
        "/edit/alpha",
        headers={"Authorization": f"Basic {creds}"},
        data={
            "luid": "LUID-1",
            "slug": "alpha",
            "name": "Alpha PROD",
            "owner": "alpha-owner",
            "purpose": "",
            "audience": "",
            "freshness_sla": "",
            "description": "",
            "how_built": "",
            "notes": "",
            "kpis": "one\ntwo",
            "glossary": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    # ttl=0 гарантирует, что следующий lookup сходит в админку и увидит новое имя.
    assert cat.lookup("alpha")["name"] == "Alpha PROD"


def test_write_api_full_crud_visible_to_catalog(
    catalog_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CRUD через JSON API + чтение свежих данных через HttpCatalog."""
    admin_client = TestClient(admin.build_app())

    def routed_get(url: str, **kwargs):
        kwargs.pop("timeout", None)
        return admin_client.get(httpx.URL(url).path, **kwargs)

    monkeypatch.setattr("dashboard_context.catalog.httpx.get", routed_get)
    cat = HttpCatalog("http://admin/api/dashboards", ttl_seconds=0.0)

    import base64
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:x").decode()}

    # CREATE — 201, catalog видит новую запись.
    r = admin_client.post(
        "/api/dashboards",
        headers=auth,
        json={"luid": "L-99", "slug": "gamma", "name": "Gamma", "kpis": ["k1"]},
    )
    assert r.status_code == 201
    assert cat.lookup("gamma")["name"] == "Gamma"

    # UPDATE по luid — 200, изменения видны.
    r = admin_client.put(
        "/api/dashboards/L-99",
        headers=auth,
        json={"luid": "L-99", "slug": "gamma", "name": "Gamma v2", "kpis": ["k1", "k2"]},
    )
    assert r.status_code == 200
    assert cat.lookup("gamma")["kpis"] == ["k1", "k2"]

    # DELETE по slug — 204, каталог больше не видит.
    r = admin_client.delete("/api/dashboards/gamma", headers=auth)
    assert r.status_code == 204
    assert cat.lookup("gamma") is None

    # Изначальные записи не тронуты.
    assert cat.lookup("alpha")["name"] == "Alpha"
