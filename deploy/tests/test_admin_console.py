"""Тесты единой админ-консоли: вход, ролевое разделение, прокси, переписывание ссылок."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from admin_console import console


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONSOLE_SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("CONSOLE_ANALYST_PASSWORD", "analyst-pw")
    monkeypatch.setenv("CONSOLE_ACCESS_ADMIN_PASSWORD", "access-pw")
    monkeypatch.setenv("CONSOLE_ADMIN_PASSWORD", "admin-pw")


@pytest.fixture
def client(env: None) -> TestClient:
    # не следуем редиректам — проверяем сами коды/куки
    return TestClient(console.build_app(), follow_redirects=False)


def _login(client: TestClient, role: str, pw: str) -> TestClient:
    r = client.post("/login", data={"role": role, "password": pw})
    assert r.status_code == 303
    return client


# ── вход ──
def test_login_wrong_password(client: TestClient) -> None:
    r = client.post("/login", data={"role": "analyst", "password": "nope"})
    assert r.status_code == 200 and "Неверная" in r.text
    assert console.COOKIE not in r.cookies


def test_login_sets_signed_cookie(client: TestClient) -> None:
    r = client.post("/login", data={"role": "analyst", "password": "analyst-pw"})
    assert r.status_code == 303
    assert r.cookies.get(console.COOKIE, "").startswith("analyst.")


def test_unauthenticated_redirects_to_login(client: TestClient) -> None:
    assert client.get("/").status_code == 303
    assert client.get("/context/").status_code == 303  # раздел тоже под входом


def test_tampered_cookie_rejected(client: TestClient) -> None:
    client.cookies.set(console.COOKIE, "admin.deadbeef")  # подделанная подпись
    assert client.get("/").status_code == 303  # трактуется как неавторизован


# ── ролевое разделение (главное) ──
def test_analyst_home_hides_pat_section(client: TestClient) -> None:
    _login(client, "analyst", "analyst-pw")
    home = client.get("/").text
    assert "Бизнес-контекст" in home and "презентаций" in home
    assert "PAT" not in home  # аналитик не видит раздел привязок


def test_analyst_denied_identity_section(client: TestClient) -> None:
    _login(client, "analyst", "analyst-pw")
    r = client.get("/identity/")
    assert r.status_code == 403  # жёсткий гейтинг, а не скрытая ссылка


def test_access_admin_denied_context_and_allowed_identity(client: TestClient, monkeypatch) -> None:
    _login(client, "access-admin", "access-pw")
    assert client.get("/context/").status_code == 403
    # identity разрешён — но бэкенда нет, поэтому мокаем прокси-клиент (см. ниже)
    _mock_backend(monkeypatch)
    assert client.get("/identity/").status_code == 200


def test_unknown_section_404(client: TestClient) -> None:
    _login(client, "admin", "admin-pw")
    assert client.get("/nope/").status_code == 404


# ── прокси + переписывание ссылок ──
class _FakeResp:
    def __init__(self, body: bytes, ctype: str = "text/html", status: int = 200, location: str | None = None):
        self.status_code = status
        self.content = body
        self.headers = {"content-type": ctype}
        if location:
            self.headers["location"] = location


class _FakeClient:
    last: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, headers=None, **kw):
        _FakeClient.last = {"method": method, "url": url, "headers": headers or {}}
        return _FakeResp(b'<a href="/new">n</a> <form action="/edit/x"></form> <a href="//cdn/x">c</a>')


def _mock_backend(monkeypatch) -> None:
    monkeypatch.setattr(console.httpx, "AsyncClient", _FakeClient)


def test_proxy_injects_auth_and_rewrites_links(client: TestClient, monkeypatch) -> None:
    _login(client, "admin", "admin-pw")
    _mock_backend(monkeypatch)
    r = client.get("/context/edit/sales")
    assert r.status_code == 200
    # корневые ссылки ушли под префикс раздела, протокол-относительные не тронуты
    assert 'href="/context/new"' in r.text
    assert 'action="/context/edit/x"' in r.text
    assert 'href="//cdn/x"' in r.text
    # к бэкенду ушёл Basic-auth и правильный под-путь
    assert _FakeClient.last["url"].endswith("/edit/sales")
    assert _FakeClient.last["headers"]["Authorization"].startswith("Basic ")


def test_proxy_rewrites_redirect_location(client: TestClient, monkeypatch) -> None:
    _login(client, "admin", "admin-pw")

    class _RedirClient(_FakeClient):
        async def request(self, method, url, headers=None, **kw):
            return _FakeResp(b"", ctype="text/html", status=303, location="/")

    monkeypatch.setattr(console.httpx, "AsyncClient", _RedirClient)
    r = client.post("/context/edit/sales", data={"x": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/context/"  # редирект бэкенда под префикс


def test_rewrite_html_unit() -> None:
    out = console._rewrite_html(b'<a href="/">h</a><button formaction="/history/v/restore">', "/context").decode()
    assert 'href="/context/"' in out
    assert 'formaction="/context/history/v/restore"' in out
