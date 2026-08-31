"""Тесты Postgres-хранилища PAT-маппингов. Идут только при IDENTITY_PGSTORE_TEST_URL."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("psycopg", reason="psycopg не установлен")
from tableau_identity import pgstore  # noqa: E402
from tableau_identity.store import MappingError, MappingStore, generate_key  # noqa: E402

TEST_URL = os.environ.get("IDENTITY_PGSTORE_TEST_URL")


@pytest.fixture
def db():
    if not TEST_URL:
        pytest.skip("IDENTITY_PGSTORE_TEST_URL не задан")
    pgstore.init(TEST_URL)
    with pgstore._connect(TEST_URL) as c, c.cursor() as cur:
        cur.execute("TRUNCATE pat_mappings")
        c.commit()
    return TEST_URL


def _store(db, key=None) -> MappingStore:
    return MappingStore("/tmp/identity-unused.yml", key or generate_key(), db_url=db)


def test_pg_roundtrip_and_secret_encrypted(db) -> None:
    s = _store(db)
    s.put("alice@corp", "alice@tab", "alice-pat", "secret-123")
    m = s.get("alice@corp")
    assert m is not None and m.pat_secret == "secret-123" and m.tableau_username == "alice@tab"
    # В БД секрет — только шифртекст.
    raw = pgstore.read_all(db)
    assert "secret-123" not in raw["alice@corp"]["pat_secret_enc"]


def test_pg_one_to_one_conflict(db) -> None:
    s = _store(db)
    s.put("a@corp", "shared", "pa", "sa")
    with pytest.raises(MappingError):
        s.put("b@corp", "shared", "pb", "sb")  # та же учётка Tableau на двоих


def test_pg_update_same_user_ok(db) -> None:
    s = _store(db)
    s.put("a@corp", "a@tab", "pa", "sa")
    s.put("a@corp", "a@tab", "pa2", "sa2")
    assert s.get("a@corp").pat_name == "pa2" and s.get("a@corp").pat_secret == "sa2"


def test_pg_delete(db) -> None:
    s = _store(db)
    s.put("a@corp", "a@tab", "p", "s")
    assert s.delete("a@corp") is True
    assert s.get("a@corp") is None
    assert s.delete("nobody") is False


def test_pg_migrate_from_file_keeps_secret(db, tmp_path) -> None:
    key = generate_key()
    file_store = MappingStore(tmp_path / "m.yml", key)              # файловый режим
    file_store.put("c@corp", "c@tab", "cp", "c-secret")
    pg_store = MappingStore(tmp_path / "m.yml", key, db_url=db)     # тот же ключ, PG-режим
    assert pg_store.migrate_from_file_if_empty() == 1
    m = pg_store.get("c@corp")
    assert m is not None and m.pat_secret == "c-secret"            # шифртекст перенесён и читается
    assert pg_store.migrate_from_file_if_empty() == 0              # повторно не мигрирует
