"""Хранилище бизнес-контекста дашбордов в Postgres (ТС «каталог в БД»).

Включается переменной `DASHBOARD_DATABASE_URL` (или `DATABASE_URL`). Если не
задана — админка работает по-старому с YAML-файлом (обратная совместимость).

Модель:
- `dashboards(key, data jsonb, created_at, updated_at)` — сам каталог, запись как
  JSONB (гибкая форма, совпадает с YAML).
- `dashboard_history(id, key, data jsonb, taken_at)` — история НА УРОВНЕ ЗАПИСИ:
  при каждом изменении в history пишется ПРЕДЫДУЩее состояние изменившейся записи.
  Отсюда per-entry таймлайн берётся напрямую из БД.

Драйвер — psycopg3 (sync): админка низконагруженная, синхронного доступа хватает.
"""
from __future__ import annotations

import os
import urllib.parse
from datetime import datetime
from typing import Any

try:  # psycopg нужен только в PG-режиме; YAML-режим работает без него
    import psycopg
    from psycopg.types.json import Json
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]
    Json = None  # type: ignore[assignment]


def database_url() -> str | None:
    return os.environ.get("DASHBOARD_DATABASE_URL") or os.environ.get("DATABASE_URL")


def enabled() -> bool:
    return bool(database_url())


def _connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url())  # type: ignore[arg-type]


def _key(e: dict[str, Any]) -> str:
    return str(e.get("slug") or e.get("luid") or "")


def _ensure_database() -> None:
    """Создать целевую БД, если её ещё нет (подключаясь к служебной `postgres`)."""
    url = database_url()
    assert url
    parsed = urllib.parse.urlparse(url)
    dbname = parsed.path.lstrip("/") or "dashboard_context"
    admin_url = parsed._replace(path="/postgres").geturl()
    with _connect(admin_url) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{dbname}"')


def init() -> None:
    """Создать БД и схему (идемпотентно). Зовётся на старте админки."""
    if not enabled():
        return
    _ensure_database()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS dashboards(
                 key        text PRIMARY KEY,
                 data       jsonb NOT NULL,
                 created_at timestamptz NOT NULL DEFAULT now(),
                 updated_at timestamptz NOT NULL DEFAULT now())"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_history(
                 id       bigserial PRIMARY KEY,
                 key      text NOT NULL,
                 data     jsonb NOT NULL,
                 taken_at timestamptz NOT NULL DEFAULT now())"""
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dch_key ON dashboard_history(key, taken_at DESC)")
        conn.commit()


def is_empty() -> bool:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM dashboards LIMIT 1")
        return cur.fetchone() is None


def load_all() -> list[dict[str, Any]]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT data FROM dashboards ORDER BY created_at, key")
        return [row[0] for row in cur.fetchall()]


def save_all(entries: list[dict[str, Any]]) -> None:
    """Полное состояние каталога → БД. Изменившиеся/удалённые записи перед
    перезаписью снимаются в history (per-entry версия)."""
    new: dict[str, dict[str, Any]] = {}
    for e in entries:
        k = _key(e)
        if k:
            new[k] = e
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT key, data FROM dashboards")
        old = {k: d for k, d in cur.fetchall()}
        for k, d in old.items():
            if k not in new or new[k] != d:
                cur.execute("INSERT INTO dashboard_history(key, data) VALUES(%s, %s)", (k, Json(d)))
        for k, e in new.items():
            cur.execute(
                """INSERT INTO dashboards(key, data) VALUES(%s, %s)
                   ON CONFLICT(key) DO UPDATE SET data = EXCLUDED.data, updated_at = now()""",
                (k, Json(e)),
            )
        removed = set(old) - set(new)
        if removed:
            cur.execute("DELETE FROM dashboards WHERE key = ANY(%s)", (list(removed),))
        conn.commit()


def entry_history(key: str) -> list[tuple[str, datetime, dict[str, Any]]]:
    """История одной записи: (version_id, когда, состояние) от новых к старым."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, taken_at, data FROM dashboard_history WHERE key = %s ORDER BY taken_at DESC, id DESC",
            (key,),
        )
        return [(str(i), t, d) for i, t, d in cur.fetchall()]


def entry_at(version: str, key: str) -> dict[str, Any] | None:
    if not version.isdigit():
        return None
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT data FROM dashboard_history WHERE id = %s AND key = %s", (int(version), key))
        row = cur.fetchone()
        return row[0] if row else None


def recent_changes(limit: int = 100) -> list[tuple[str, str, datetime]]:
    """Недавние изменения по каталогу: (version_id, key, когда)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, key, taken_at FROM dashboard_history ORDER BY taken_at DESC, id DESC LIMIT %s",
            (limit,),
        )
        return [(str(i), k, t) for i, k, t in cur.fetchall()]
