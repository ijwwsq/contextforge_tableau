"""Read-side view of the versioned dashboard catalog (YAML).

The catalog is the source of truth for *business* context that Tableau itself
doesn't hold: what the dashboard is for, who owns it, freshness expectations,
KPIs and glossary terms. It is meant to be edited in the repo and reviewed
via PR.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml


class Catalog:
    """Reload-on-mtime YAML catalog keyed by dashboard LUID."""

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
            self._mtime = mtime

    def lookup(self, key: str) -> dict[str, Any] | None:
        self._reload_if_changed()
        return self._by_luid.get(key) or self._by_slug.get(key)

    def all(self) -> list[dict[str, Any]]:
        self._reload_if_changed()
        return list(self._by_luid.values()) or list(self._by_slug.values())


def default_catalog() -> Catalog:
    path = os.environ.get("CATALOG_PATH", "/app/catalog/dashboards.yml")
    return Catalog(path)
