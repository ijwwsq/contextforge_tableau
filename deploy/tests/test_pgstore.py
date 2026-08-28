"""Тесты Postgres-хранилища каталога. Идут только если задан PGSTORE_TEST_URL
(URL к тестовой БД), иначе пропускаются — в обычном CI без Postgres."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("psycopg", reason="psycopg не установлен")
from dashboard_context import pgstore  # noqa: E402

TEST_URL = os.environ.get("PGSTORE_TEST_URL")


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch):
    if not TEST_URL:
        pytest.skip("PGSTORE_TEST_URL не задан")
    monkeypatch.setenv("DASHBOARD_DATABASE_URL", TEST_URL)
    pgstore.init()
    with pgstore._connect() as c, c.cursor() as cur:
        cur.execute("TRUNCATE dashboards, dashboard_history")
        c.commit()
    return TEST_URL


def test_save_load_roundtrip(db) -> None:
    pgstore.save_all([{"slug": "a", "name": "A", "owner": "x", "kpis": ["MRR"]}])
    got = pgstore.load_all()
    assert len(got) == 1
    assert got[0]["slug"] == "a" and got[0]["owner"] == "x" and got[0]["kpis"] == ["MRR"]


def test_history_captures_previous_state_on_change(db) -> None:
    pgstore.save_all([{"slug": "a", "owner": "v1@e"}])
    pgstore.save_all([{"slug": "a", "owner": "v2@e"}])
    hist = pgstore.entry_history("a")
    assert len(hist) == 1                      # прежнее состояние (v1) ушло в историю
    vid, _when, data = hist[0]
    assert data["owner"] == "v1@e"
    assert pgstore.entry_at(vid, "a")["owner"] == "v1@e"
    assert pgstore.load_all()[0]["owner"] == "v2@e"   # актуальное — v2


def test_delete_snapshots_history(db) -> None:
    pgstore.save_all([{"slug": "a", "owner": "v1"}])
    pgstore.save_all([])                       # запись удалена
    assert pgstore.load_all() == []
    assert len(pgstore.entry_history("a")) == 1  # но её прежнее состояние в истории


def test_recent_changes_lists_edits(db) -> None:
    pgstore.save_all([{"slug": "a", "owner": "1"}])
    pgstore.save_all([{"slug": "a", "owner": "2"}])
    pgstore.save_all([{"slug": "a", "owner": "3"}])
    changes = pgstore.recent_changes()
    assert len(changes) == 2 and all(k == "a" for _v, k, _t in changes)
