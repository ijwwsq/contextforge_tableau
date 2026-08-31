"""Postgres-хранилище 1:1-маппинга «пользователь → учётка Tableau (PAT)».

Включается `IDENTITY_DATABASE_URL`. Секрет PAT лежит в БД так же зашифрованным
(Fernet-шифртекст в `pat_secret_enc`) — шифрование делает MappingStore, БД хранит
только шифртекст. Инвариант 1:1 подкреплён UNIQUE(tableau_username).

Драйвер — psycopg3 (sync). Импорт ленивый: YAML-режим работает без psycopg.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]


def _connect(url: str) -> "psycopg.Connection":
    return psycopg.connect(url)


def _ensure_database(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    dbname = parsed.path.lstrip("/") or "tableau_identity"
    admin_url = parsed._replace(path="/postgres").geturl()
    with _connect(admin_url) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{dbname}"')


def init(url: str) -> None:
    _ensure_database(url)
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS pat_mappings(
                 "user"           text PRIMARY KEY,
                 tableau_username text NOT NULL UNIQUE,
                 pat_name         text NOT NULL,
                 pat_secret_enc   text NOT NULL,
                 updated_at       timestamptz NOT NULL DEFAULT now())"""
        )
        conn.commit()


def read_all(url: str) -> dict[str, dict[str, Any]]:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute('SELECT "user", tableau_username, pat_name, pat_secret_enc FROM pat_mappings')
        return {
            u: {"user": u, "tableau_username": tu, "pat_name": pn, "pat_secret_enc": enc}
            for u, tu, pn, enc in cur.fetchall()
        }


def upsert(url: str, user: str, tableau_username: str, pat_name: str, pat_secret_enc: str) -> str | None:
    """Вставка/обновление привязки. Если учётка Tableau занята ДРУГИМ юзером —
    возвращает его (конфликт 1:1), запись не делается. Иначе None."""
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute('SELECT "user" FROM pat_mappings WHERE tableau_username = %s AND "user" <> %s',
                    (tableau_username, user))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            """INSERT INTO pat_mappings("user", tableau_username, pat_name, pat_secret_enc)
               VALUES(%s, %s, %s, %s)
               ON CONFLICT("user") DO UPDATE SET
                 tableau_username = EXCLUDED.tableau_username,
                 pat_name = EXCLUDED.pat_name,
                 pat_secret_enc = EXCLUDED.pat_secret_enc,
                 updated_at = now()""",
            (user, tableau_username, pat_name, pat_secret_enc),
        )
        conn.commit()
        return None


def delete(url: str, user: str) -> bool:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute('DELETE FROM pat_mappings WHERE "user" = %s', (user,))
        conn.commit()
        return cur.rowcount > 0


def is_empty(url: str) -> bool:
    with _connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pat_mappings LIMIT 1")
        return cur.fetchone() is None
