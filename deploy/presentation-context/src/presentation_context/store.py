"""Read the single presentation guide from the admin API or a local YAML file."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import yaml


class GuideStore:
    def __init__(self, url: str | None = None, path: str | Path | None = None, ttl_seconds: float = 5.0):
        self._url = url
        self._path = Path(path) if path else None
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._fetched_at = 0.0
        self._guide: dict[str, Any] = {}

    def get(self) -> dict[str, Any]:
        if time.monotonic() - self._fetched_at < self._ttl:
            return self._guide
        with self._lock:
            if time.monotonic() - self._fetched_at < self._ttl:
                return self._guide
            try:
                if self._url:
                    guide = httpx.get(self._url, timeout=5.0).json()
                elif self._path:
                    guide = yaml.safe_load(self._path.read_text(encoding="utf-8"))
                else:
                    guide = {}
                if isinstance(guide, dict):
                    self._guide = guide
            except Exception:
                pass  # Keep the last valid guide during an admin restart.
            self._fetched_at = time.monotonic()
        return self._guide


def default_store() -> GuideStore:
    return GuideStore(
        url=os.environ.get("GUIDELINES_URL"),
        path=os.environ.get("GUIDELINES_PATH", "/app/catalog/presentation-guidelines.yml"),
    )
