"""Тесты версионирования гайда презентаций (снимок на save + history + restore)."""
from __future__ import annotations

import base64
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from presentation_context import admin


@pytest.fixture
def guide(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "guide.yml"
    p.write_text("style_guide:\n  voice:\n    - factual\n", encoding="utf-8")
    monkeypatch.setenv("GUIDELINES_PATH", str(p))
    monkeypatch.setenv("PRESENTATION_ADMIN_PASSWORD", "secret")
    return p


def _auth() -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}


def test_history_snapshot_on_save_and_restore(guide: Path) -> None:
    c = TestClient(admin.build_app())
    # Правка → прежнее состояние уходит в историю.
    c.post("/", headers=_auth(), data={"guide": "style_guide:\n  voice:\n    - bold\n"}, follow_redirects=False)
    versions = admin._list_versions()
    assert len(versions) == 1
    assert "factual" in admin._version_path(versions[0]).read_text()   # снимок = прежний гайд

    # Restore возвращает прежнюю версию (и сам уходит в историю → обратимо).
    r = c.post(f"/history/{versions[0]}/restore", headers=_auth(), follow_redirects=False)
    assert r.status_code == 303
    assert "factual" in guide.read_text()
    assert len(admin._list_versions()) == 2


def test_history_page_and_download_under_auth(guide: Path) -> None:
    c = TestClient(admin.build_app())
    c.post("/", headers=_auth(), data={"guide": "style_guide:\n  voice:\n    - x\n"})
    v = admin._list_versions()[0]
    assert c.get("/history").status_code == 401                         # под basic-auth
    assert c.get("/history", headers=_auth()).status_code == 200
    assert "factual" in c.get(f"/history/{v}", headers=_auth()).text     # скачивание версии


def test_history_version_path_rejects_traversal() -> None:
    assert admin._version_path("../../etc/passwd") is None
    assert admin._version_path("nope") is None
