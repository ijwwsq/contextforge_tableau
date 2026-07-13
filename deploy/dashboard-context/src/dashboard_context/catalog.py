"""Чтение каталога дашбордов.

Поддерживаются два источника:

- `HttpCatalog` — забирает JSON из сервиса dashboard-context-admin
  (`GET {CATALOG_URL}` возвращает `{"dashboards": [...]}`). Дефолт в
  деплое: админка владеет хранилищем, все остальные читают через неё.
  Выбирается, когда переменная окружения `CATALOG_URL` задана.
- `YamlCatalog` — читает YAML-файл напрямую с диска. Оставлен как fallback
  для локальных запусков без админки, а также для самой админки (она с
  собой по HTTP не общается).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

log = logging.getLogger("dashboard_context.catalog")


class Catalog(Protocol):
    def lookup(self, key: str) -> dict[str, Any] | None: ...
    def all(self) -> list[dict[str, Any]]: ...


class YamlCatalog:
    """YAML-каталог с перечитыванием по mtime, индексируется по LUID и slug."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._mtime: float = 0.0
        self._by_luid: dict[str, dict[str, Any]] = {}
        self._by_slug: dict[str, dict[str, Any]] = {}

    def _reload_if_changed(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            with self._lock:
                self._by_luid = {}
                self._by_slug = {}
                self._mtime = 0.0
            return

        if mtime == self._mtime:
            return

        with self._lock:
            if mtime == self._mtime:
                return
            data = yaml.safe_load(self._path.read_text()) or {}
            entries = data.get("dashboards", []) or []
            self._index(entries)
            self._mtime = mtime

    def _index(self, entries: list[dict[str, Any]]) -> None:
        by_luid: dict[str, dict[str, Any]] = {}
        by_slug: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if luid := entry.get("luid"):
                by_luid[str(luid)] = entry
            if slug := entry.get("slug"):
                by_slug[str(slug)] = entry
        self._by_luid = by_luid
        self._by_slug = by_slug

    def lookup(self, key: str) -> dict[str, Any] | None:
        self._reload_if_changed()
        return self._by_luid.get(key) or self._by_slug.get(key)

    def all(self) -> list[dict[str, Any]]:
        self._reload_if_changed()
        return list(self._by_luid.values()) or list(self._by_slug.values())


class HttpCatalog:
    """Тянет каталог из сервиса-админки.

    Короткий TTL-кэш убирает лишний трафик с горячего пути /mcp — админка
    рядом на внутренней сети, поэтому запрос дешёвый, но за один вызов
    describe_dashboard мы обращаемся к каталогу несколько раз.
    """

    def __init__(self, url: str, *, ttl_seconds: float = 5.0, timeout: float = 5.0):
        self._url = url.rstrip("/")
        self._ttl = ttl_seconds
        self._timeout = timeout
        self._lock = threading.Lock()
        self._fetched_at: float = 0.0
        self._by_luid: dict[str, dict[str, Any]] = {}
        self._by_slug: dict[str, dict[str, Any]] = {}
        self._entries: list[dict[str, Any]] = []

    def _refresh_if_stale(self) -> None:
        if time.monotonic() - self._fetched_at < self._ttl:
            return
        with self._lock:
            if time.monotonic() - self._fetched_at < self._ttl:
                return
            try:
                resp = httpx.get(self._url, timeout=self._timeout)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                # Отдаём последнюю удачную копию вместо пустого результата —
                # разовые рестарты админки не должны ломать describe_dashboard.
                log.warning("catalog fetch from %s failed: %s (serving cached copy)", self._url, exc)
                self._fetched_at = time.monotonic()
                return

            entries = payload.get("dashboards") if isinstance(payload, dict) else payload
            if not isinstance(entries, list):
                log.warning("catalog fetch returned unexpected shape: %r", payload)
                self._fetched_at = time.monotonic()
                return

            self._entries = [e for e in entries if isinstance(e, dict)]
            by_luid: dict[str, dict[str, Any]] = {}
            by_slug: dict[str, dict[str, Any]] = {}
            for entry in self._entries:
                if luid := entry.get("luid"):
                    by_luid[str(luid)] = entry
                if slug := entry.get("slug"):
                    by_slug[str(slug)] = entry
            self._by_luid = by_luid
            self._by_slug = by_slug
            self._fetched_at = time.monotonic()

    def lookup(self, key: str) -> dict[str, Any] | None:
        self._refresh_if_stale()
        return self._by_luid.get(key) or self._by_slug.get(key)

    def all(self) -> list[dict[str, Any]]:
        self._refresh_if_stale()
        return list(self._entries)


def default_catalog() -> Catalog:
    url = os.environ.get("CATALOG_URL")
    if url:
        log.info("catalog source: admin API at %s", url)
        return HttpCatalog(url)
    path = os.environ.get("CATALOG_PATH", "/app/catalog/dashboards.yml")
    log.info("catalog source: YAML at %s", path)
    return YamlCatalog(path)
