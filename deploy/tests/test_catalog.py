"""Тесты для обоих бэкендов каталога, которые может читать MCP-сервер."""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from dashboard_context.catalog import HttpCatalog, YamlCatalog, default_catalog


# ---------------------------------------------------------------- YamlCatalog


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_yaml_catalog_indexes_by_luid_and_slug(tmp_path: Path) -> None:
    catalog_file = tmp_path / "dashboards.yml"
    _write_yaml(
        catalog_file,
        """
dashboards:
  - luid: abc-123
    slug: sales-weekly
    name: Sales weekly
    owner: analytics
  - luid: def-456
    slug: ops-nightly
    name: Ops nightly
""",
    )
    catalog = YamlCatalog(catalog_file)

    assert catalog.lookup("abc-123")["name"] == "Sales weekly"
    assert catalog.lookup("sales-weekly")["name"] == "Sales weekly"
    assert catalog.lookup("does-not-exist") is None
    assert {e["name"] for e in catalog.all()} == {"Sales weekly", "Ops nightly"}


def test_yaml_catalog_reloads_when_mtime_changes(tmp_path: Path) -> None:
    catalog_file = tmp_path / "dashboards.yml"
    _write_yaml(catalog_file, "dashboards:\n  - luid: v1\n    name: first\n")
    catalog = YamlCatalog(catalog_file)
    assert catalog.lookup("v1")["name"] == "first"

    # На некоторых ФС разрешение mtime — 1 секунда, поэтому явно двигаем время вперёд,
    # чтобы гарантированно триггернуть перечитывание файла.
    _write_yaml(catalog_file, "dashboards:\n  - luid: v1\n    name: second\n")
    import os
    future = time.time() + 5
    os.utime(catalog_file, (future, future))

    assert catalog.lookup("v1")["name"] == "second"


def test_yaml_catalog_missing_file_is_empty(tmp_path: Path) -> None:
    catalog = YamlCatalog(tmp_path / "missing.yml")
    assert catalog.all() == []
    assert catalog.lookup("anything") is None


def test_yaml_catalog_skips_non_dict_entries(tmp_path: Path) -> None:
    catalog_file = tmp_path / "dashboards.yml"
    _write_yaml(
        catalog_file,
        """
dashboards:
  - luid: ok
    name: ok
  - just a string
  - luid: also-ok
    name: also ok
""",
    )
    catalog = YamlCatalog(catalog_file)
    assert {e["name"] for e in catalog.all()} == {"ok", "also ok"}


# ---------------------------------------------------------------- HttpCatalog


def _install_http_client(monkeypatch, responder):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return responder(request, calls["n"])

    transport = httpx.MockTransport(handler)

    # HttpCatalog использует module-level httpx.get() — патчим его напрямую,
    # чтобы весь трафик уходил в MockTransport вместо реальной сети.
    def fake_get(url: str, **kwargs):
        with httpx.Client(transport=transport) as c:
            return c.get(url, **kwargs)

    monkeypatch.setattr("dashboard_context.catalog.httpx.get", fake_get)
    return calls


def test_http_catalog_fetches_and_indexes(monkeypatch) -> None:
    def responder(request, n):
        assert request.url.path == "/api/dashboards"
        return httpx.Response(
            200,
            json={
                "dashboards": [
                    {"luid": "aaa", "slug": "s1", "name": "One"},
                    {"luid": "bbb", "slug": "s2", "name": "Two"},
                ]
            },
        )

    calls = _install_http_client(monkeypatch, responder)
    cat = HttpCatalog("http://admin/api/dashboards", ttl_seconds=0.0)

    assert cat.lookup("aaa")["name"] == "One"
    assert cat.lookup("s2")["name"] == "Two"
    assert cat.lookup("unknown") is None
    assert len(cat.all()) == 2
    # ttl=0 означает, что каждый вызов идёт в сеть заново.
    assert calls["n"] >= 3


def test_http_catalog_ttl_cache_prevents_refetch(monkeypatch) -> None:
    def responder(request, n):
        return httpx.Response(200, json={"dashboards": [{"luid": "x", "name": "cached"}]})

    calls = _install_http_client(monkeypatch, responder)
    cat = HttpCatalog("http://admin/api/dashboards", ttl_seconds=60.0)

    for _ in range(5):
        assert cat.lookup("x")["name"] == "cached"
    assert calls["n"] == 1  # в сеть сходили только один раз, всё остальное — из кэша


def test_http_catalog_serves_stale_data_on_error(monkeypatch, caplog) -> None:
    responses = [
        httpx.Response(200, json={"dashboards": [{"luid": "x", "name": "fresh"}]}),
        httpx.Response(500, text="boom"),
    ]
    idx = {"n": 0}

    def responder(request, _n):
        r = responses[min(idx["n"], len(responses) - 1)]
        idx["n"] += 1
        return r

    _install_http_client(monkeypatch, responder)
    cat = HttpCatalog("http://admin/api/dashboards", ttl_seconds=0.0)

    assert cat.lookup("x")["name"] == "fresh"
    # Второй вызов перезапрашивает (ttl=0), но эндпоинт отвечает 500 —
    # HttpCatalog обязан отдать закэшированную копию, а не пустой результат.
    with caplog.at_level("WARNING"):
        assert cat.lookup("x")["name"] == "fresh"
    assert any("catalog fetch" in rec.message for rec in caplog.records)


def test_http_catalog_ignores_bad_shape(monkeypatch) -> None:
    def responder(request, _n):
        return httpx.Response(200, json={"unexpected": "shape"})

    _install_http_client(monkeypatch, responder)
    cat = HttpCatalog("http://admin/api/dashboards", ttl_seconds=0.0)
    # Ключа `dashboards` нет — каталог остаётся пустым, падать нельзя.
    assert cat.all() == []


def test_http_catalog_accepts_bare_list_payload(monkeypatch) -> None:
    def responder(request, _n):
        return httpx.Response(200, json=[{"luid": "z", "name": "bare"}])

    _install_http_client(monkeypatch, responder)
    cat = HttpCatalog("http://admin/api/dashboards", ttl_seconds=0.0)
    assert cat.lookup("z")["name"] == "bare"


# ---------------------------------------------------------------- selection


def test_default_catalog_prefers_http_when_configured(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CATALOG_URL", "http://admin/api/dashboards")
    monkeypatch.setenv("CATALOG_PATH", str(tmp_path / "unused.yml"))
    cat = default_catalog()
    assert isinstance(cat, HttpCatalog)


def test_default_catalog_falls_back_to_yaml(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CATALOG_URL", raising=False)
    monkeypatch.setenv("CATALOG_PATH", str(tmp_path / "dashboards.yml"))
    cat = default_catalog()
    assert isinstance(cat, YamlCatalog)
