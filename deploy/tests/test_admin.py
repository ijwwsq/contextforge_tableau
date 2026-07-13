"""Тесты для admin-сервиса: JSON API, редактор форм, Basic Auth."""
from __future__ import annotations

import base64
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dashboard_context import admin


@pytest.fixture
def catalog_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный YAML-каталог, куда admin будет писать в каждом тесте."""
    path = tmp_path / "dashboards.yml"
    path.write_text(
        """
dashboards:
  - luid: aaa-111
    slug: sales
    name: Sales weekly
    owner: analytics@example.com
    kpis:
      - MRR
      - Churn
    glossary:
      MRR: monthly recurring revenue
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("CATALOG_PATH", str(path))
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
    return path


@pytest.fixture
def client(catalog_file: Path) -> TestClient:
    return TestClient(admin.build_app())


def _basic(user: str = "admin", pwd: str = "s3cret") -> dict[str, str]:
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# ---------------------------------------------------------------- JSON API


def test_api_list_returns_all_entries_without_auth(client: TestClient) -> None:
    # Ручка нужна MCP-серверу, авторизация на /api/* умышленно выключена.
    r = client.get("/api/dashboards")
    assert r.status_code == 200
    payload = r.json()
    assert isinstance(payload["dashboards"], list)
    assert payload["dashboards"][0]["luid"] == "aaa-111"
    assert payload["dashboards"][0]["kpis"] == ["MRR", "Churn"]
    assert payload["dashboards"][0]["glossary"] == {"MRR": "monthly recurring revenue"}


def test_api_get_by_luid(client: TestClient) -> None:
    r = client.get("/api/dashboards/aaa-111")
    assert r.status_code == 200
    assert r.json()["name"] == "Sales weekly"


def test_api_get_by_slug(client: TestClient) -> None:
    r = client.get("/api/dashboards/sales")
    assert r.status_code == 200
    assert r.json()["luid"] == "aaa-111"


def test_api_get_missing_returns_404(client: TestClient) -> None:
    r = client.get("/api/dashboards/nope")
    assert r.status_code == 404
    assert r.json() == {"error": "not_found"}


def test_api_returns_plain_json_types(client: TestClient) -> None:
    # ruamel-типы (CommentedMap/LiteralScalarString) не должны просачиваться в JSON.
    payload = client.get("/api/dashboards").json()
    entry = payload["dashboards"][0]
    assert type(entry["name"]) is str
    assert type(entry["kpis"]) is list
    assert type(entry["kpis"][0]) is str


# ---------------------------------------------------------------- Health & export


def test_healthz_ok(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["entries"] == 1
    assert body["catalog_path"].endswith(".yml")


def test_healthz_no_auth(client: TestClient) -> None:
    # Без Authorization-заголовка тоже должен отвечать 200 — иначе Docker healthcheck сломается.
    r = client.get("/healthz")
    assert r.status_code == 200


def test_api_export_returns_yaml(client: TestClient) -> None:
    r = client.get("/api/export")
    assert r.status_code == 200
    assert "yaml" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    assert "sales" in r.text  # slug из фикстуры попал в YAML

    # Ответ действительно валидный YAML.
    import yaml
    parsed = yaml.safe_load(r.text)
    assert parsed["dashboards"][0]["slug"] == "sales"


# ---------------------------------------------------------------- Write API


def test_api_create_new_entry(client: TestClient) -> None:
    r = client.post(
        "/api/dashboards",
        headers=_basic(),
        json={
            "luid": "new-luid",
            "slug": "brand-new",
            "name": "Brand new",
            "owner": "owner@example.com",
            "kpis": ["A", "B"],
            "glossary": {"K": "V"},
        },
    )
    assert r.status_code == 201
    assert r.json()["name"] == "Brand new"

    # Появилось в списке.
    listed = client.get("/api/dashboards").json()["dashboards"]
    assert any(e["slug"] == "brand-new" for e in listed)


def test_api_create_requires_auth(client: TestClient) -> None:
    r = client.post("/api/dashboards", json={"slug": "x"})
    assert r.status_code == 401


def test_api_create_rejects_invalid_json(client: TestClient) -> None:
    r = client.request(
        "POST",
        "/api/dashboards",
        headers={**_basic(), "Content-Type": "application/json"},
        content=b"not json at all",
    )
    assert r.status_code == 400
    assert "invalid JSON" in r.json()["error"]


def test_api_create_rejects_missing_identifiers(client: TestClient) -> None:
    r = client.post("/api/dashboards", headers=_basic(), json={"name": "no key"})
    assert r.status_code == 400
    assert r.json()["error"] == "validation_failed"
    assert any("luid" in d for d in r.json()["details"])


def test_api_create_rejects_bad_slug(client: TestClient) -> None:
    r = client.post(
        "/api/dashboards",
        headers=_basic(),
        json={"slug": "Sales Weekly!", "name": "n"},
    )
    assert r.status_code == 400
    assert any("kebab-case" in d for d in r.json()["details"])


def test_api_create_rejects_unknown_field(client: TestClient) -> None:
    r = client.post(
        "/api/dashboards",
        headers=_basic(),
        json={"slug": "ok", "unexpected_field": 1},
    )
    assert r.status_code == 400
    assert any("unknown" in d for d in r.json()["details"])


def test_api_create_rejects_kpis_wrong_type(client: TestClient) -> None:
    r = client.post(
        "/api/dashboards",
        headers=_basic(),
        json={"slug": "ok", "kpis": "not-an-array"},
    )
    assert r.status_code == 400
    assert any("kpis" in d for d in r.json()["details"])


def test_api_create_conflict_on_duplicate(client: TestClient) -> None:
    # sales уже есть в фикстуре.
    r = client.post(
        "/api/dashboards",
        headers=_basic(),
        json={"slug": "sales", "name": "dup"},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "already_exists"


def test_api_update_by_luid(client: TestClient) -> None:
    r = client.put(
        "/api/dashboards/aaa-111",
        headers=_basic(),
        json={
            "luid": "aaa-111",
            "slug": "sales",
            "name": "Sales (patched)",
            "kpis": ["only-one"],
        },
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Sales (patched)"
    assert r.json()["kpis"] == ["only-one"]

    # Изменение переживает reload.
    assert client.get("/api/dashboards/sales").json()["name"] == "Sales (patched)"


def test_api_update_requires_auth(client: TestClient) -> None:
    r = client.put("/api/dashboards/sales", json={"slug": "sales", "name": "n"})
    assert r.status_code == 401


def test_api_update_returns_404_for_missing(client: TestClient) -> None:
    r = client.put(
        "/api/dashboards/nope",
        headers=_basic(),
        json={"slug": "nope", "name": "n"},
    )
    assert r.status_code == 404


def test_api_delete_by_luid(client: TestClient) -> None:
    r = client.delete("/api/dashboards/aaa-111", headers=_basic())
    assert r.status_code == 204
    assert client.get("/api/dashboards").json()["dashboards"] == []


def test_api_delete_requires_auth(client: TestClient) -> None:
    r = client.delete("/api/dashboards/sales")
    assert r.status_code == 401


def test_api_delete_returns_404_for_missing(client: TestClient) -> None:
    r = client.delete("/api/dashboards/nope", headers=_basic())
    assert r.status_code == 404


def test_api_get_now_accepts_luid(client: TestClient) -> None:
    # Регрессия: раньше `_find` разрешал только slug, если тот был. Теперь работает оба.
    assert client.get("/api/dashboards/aaa-111").json()["slug"] == "sales"
    assert client.get("/api/dashboards/sales").json()["luid"] == "aaa-111"


def test_edit_by_luid_now_works(catalog_file: Path, client: TestClient) -> None:
    # Регрессия по `_find`: HTML-редактор должен уметь открыть карточку и по luid.
    r = client.get("/edit/aaa-111", headers=_basic())
    assert r.status_code == 200
    assert "Sales weekly" in r.text


# ---------------------------------------------------------------- Basic Auth


def test_html_editor_requires_auth(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_html_editor_rejects_wrong_password(client: TestClient) -> None:
    r = client.get("/", headers=_basic(pwd="wrong"))
    assert r.status_code == 401


def test_html_editor_accepts_correct_credentials(client: TestClient) -> None:
    r = client.get("/", headers=_basic())
    assert r.status_code == 200
    assert "Sales weekly" in r.text
    assert "New dashboard" in r.text


def test_build_app_refuses_to_start_without_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        admin.build_app()


# ---------------------------------------------------------------- Формы


def test_edit_updates_yaml_and_survives_reload(catalog_file: Path, client: TestClient) -> None:
    # UI формирует URL из `_entry_key` (slug приоритетнее luid), поэтому и здесь
    # используем slug — как это делает реальный редактор из /.
    r = client.post(
        "/edit/sales",
        headers=_basic(),
        data={
            "luid": "aaa-111",
            "slug": "sales",
            "name": "Sales weekly (renamed)",
            "owner": "new-owner@example.com",
            "purpose": "test",
            "audience": "eng",
            "freshness_sla": "24h",
            "description": "line1\nline2",
            "how_built": "built here",
            "notes": "",
            "kpis": "A\nB\nC",
            "glossary": "K: V\nX: Y",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    # Читаем через API — заодно проверяем, что данные пережили сериализацию.
    payload = client.get("/api/dashboards/aaa-111").json()
    assert payload["name"] == "Sales weekly (renamed)"
    assert payload["owner"] == "new-owner@example.com"
    assert payload["kpis"] == ["A", "B", "C"]
    assert payload["glossary"] == {"K": "V", "X": "Y"}
    # Пустое поле уходит из YAML, а не остаётся пустой строкой.
    assert "notes" not in payload


def test_new_entry_appends_to_catalog(catalog_file: Path, client: TestClient) -> None:
    r = client.post(
        "/new",
        headers=_basic(),
        data={
            "luid": "zzz-999",
            "slug": "new-dash",
            "name": "New dash",
            "owner": "",
            "purpose": "",
            "audience": "",
            "freshness_sla": "",
            "description": "",
            "how_built": "",
            "notes": "",
            "kpis": "",
            "glossary": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    payload = client.get("/api/dashboards").json()
    assert any(e["slug"] == "new-dash" for e in payload["dashboards"])


def test_new_entry_without_key_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/new",
        headers=_basic(),
        data={"name": "no key", "luid": "", "slug": ""},
    )
    # Нет ни slug, ни luid — сервер отдаёт форму заново с сообщением, не 303.
    assert r.status_code == 200
    assert "&#x27;luid&#x27; or &#x27;slug&#x27; is required" in r.text


def test_new_entry_with_duplicate_key_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/new",
        headers=_basic(),
        data={"luid": "", "slug": "sales", "name": "dup"},
    )
    assert r.status_code == 200
    assert "already exists" in r.text


def test_delete_removes_entry(catalog_file: Path, client: TestClient) -> None:
    r = client.post("/delete/sales", headers=_basic(), follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/dashboards").json()["dashboards"] == []


def test_edit_get_shows_existing_values(client: TestClient) -> None:
    r = client.get("/edit/sales", headers=_basic())
    assert r.status_code == 200
    # Значения из YAML попадают в атрибуты <input value="..."> и текст textarea.
    assert "Sales weekly" in r.text
    assert "MRR" in r.text


def test_edit_get_unknown_key_is_graceful(client: TestClient) -> None:
    r = client.get("/edit/nope", headers=_basic())
    assert r.status_code == 200
    assert "No dashboard" in r.text
