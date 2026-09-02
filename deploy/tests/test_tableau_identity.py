"""Тесты сервиса tableau-identity: store, tableau_auth, identity, broker, admin.

Покрывают то, что делает сервис прод-реди: шифрование PAT, инвариант 1:1,
единственный signin под пер-юзерным локом (ограничение «один PAT = одна сессия»),
инъекцию x-tableau-auth, гейтинг tools/call, retry на 401 и защиту админки.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path

import httpx
import jwt
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tableau_identity import admin, broker as bm
from tableau_identity.identity import IdentityConfig, extract_user
from tableau_identity.store import MappingError, MappingStore, generate_key
from tableau_identity import tableau_auth as ta
from tableau_identity.tableau_auth import SessionCache, TableauEndpoint, TableauSignInError


# ─────────────────────────── store ───────────────────────────
@pytest.fixture
def store(tmp_path: Path) -> MappingStore:
    return MappingStore(tmp_path / "mappings.yml", generate_key())


def test_store_roundtrip_and_secret_encrypted_on_disk(store: MappingStore, tmp_path: Path) -> None:
    store.put("alice@corp", "alice@corp", "alice-pat", "s3cr3t-value")
    m = store.get("alice@corp")
    assert m is not None
    assert (m.tableau_username, m.pat_name, m.pat_secret) == ("alice@corp", "alice-pat", "s3cr3t-value")
    # На диске секрета в открытом виде нет.
    on_disk = (tmp_path / "mappings.yml").read_text(encoding="utf-8")
    assert "s3cr3t-value" not in on_disk
    assert "pat_secret_enc" in on_disk


def test_store_public_hides_secret(store: MappingStore) -> None:
    store.put("bob@corp", "bob@corp", "bob-pat", "topsecret")
    pub = store.list()[0].public()
    assert "topsecret" not in json.dumps(pub)
    assert pub == {"user": "bob@corp", "tableau_username": "bob@corp", "pat_name": "bob-pat"}


def test_store_enforces_one_to_one(store: MappingStore) -> None:
    store.put("alice@corp", "shared-tab", "pat-a", "sa")
    with pytest.raises(MappingError):
        store.put("bob@corp", "shared-tab", "pat-b", "sb")  # та же учётка Tableau на двоих


def test_store_update_same_user_ok(store: MappingStore) -> None:
    store.put("alice@corp", "alice-tab", "pat-a", "sa")
    store.put("alice@corp", "alice-tab", "pat-a2", "sa2")  # обновление той же записи
    assert store.get("alice@corp").pat_name == "pat-a2"


def test_store_delete_and_reload(store: MappingStore) -> None:
    store.put("alice@corp", "alice-tab", "pat-a", "sa")
    assert store.delete("alice@corp") is True
    assert store.get("alice@corp") is None
    assert store.delete("nobody") is False


def test_store_wrong_key_refuses(tmp_path: Path) -> None:
    a = MappingStore(tmp_path / "m.yml", generate_key())
    a.put("alice@corp", "alice-tab", "pat", "secret")
    b = MappingStore(tmp_path / "m.yml", generate_key())  # другой ключ
    with pytest.raises(RuntimeError):
        b.get("alice@corp")


def test_store_requires_all_fields(store: MappingStore) -> None:
    with pytest.raises(MappingError):
        store.put("alice@corp", "", "pat", "secret")


# ─────────────────────────── identity ───────────────────────────
def _idcfg(**over: object) -> IdentityConfig:
    base = dict(header="authorization", secret="topsecretkey", algorithms=["HS256"],
                claim="sub", verify=True, audience=None, issuer=None)
    base.update(over)
    return IdentityConfig(**base)  # type: ignore[arg-type]


def test_identity_extracts_verified_sub() -> None:
    cfg = _idcfg()
    token = jwt.encode({"sub": "alice@corp"}, "topsecretkey", algorithm="HS256")
    assert extract_user({"authorization": f"Bearer {token}"}, cfg) == "alice@corp"


def test_identity_rejects_tampered_signature() -> None:
    cfg = _idcfg()
    token = jwt.encode({"sub": "alice@corp"}, "WRONG-KEY", algorithm="HS256")
    assert extract_user({"authorization": f"Bearer {token}"}, cfg) is None


def test_identity_missing_header_is_none() -> None:
    assert extract_user({}, _idcfg()) is None


def test_identity_no_verify_mode() -> None:
    cfg = _idcfg(verify=False, secret="")
    token = jwt.encode({"sub": "carol@corp"}, "any", algorithm="HS256")
    assert extract_user({"authorization": f"Bearer {token}"}, cfg) == "carol@corp"


def _rsa_keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()).decode()
    pub_pem = priv.public_key().public_bytes(serialization.Encoding.PEM,
                                             serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv_pem, pub_pem


def test_identity_rs256_with_public_key() -> None:
    """Асимметричная подпись (как у внешнего IdP): проверка публичным ключом."""
    priv, pub = _rsa_keypair()
    token = jwt.encode({"sub": "alice@corp"}, priv, algorithm="RS256")
    cfg = _idcfg(algorithms=["RS256"], secret="", public_key=pub)
    assert extract_user({"authorization": f"Bearer {token}"}, cfg) == "alice@corp"


def test_identity_rs256_wrong_public_key_rejected() -> None:
    priv1, _ = _rsa_keypair()
    _, pub2 = _rsa_keypair()
    token = jwt.encode({"sub": "alice@corp"}, priv1, algorithm="RS256")
    cfg = _idcfg(algorithms=["RS256"], secret="", public_key=pub2)  # чужой ключ
    assert extract_user({"authorization": f"Bearer {token}"}, cfg) is None


# ─────────────────────────── tableau_auth ───────────────────────────
def _signin_cache(handler) -> SessionCache:
    client = httpx.AsyncClient(
        base_url="https://tab.example.com/api/3.22",
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
    )
    ep = TableauEndpoint(server="https://tab.example.com", site_content_url="", api_version="3.22")
    return SessionCache(ep, client=client, ttl_seconds=1000)


async def test_signin_caches_and_returns_token() -> None:
    calls = {"signin": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/auth/signin")
        body = json.loads(req.content)["credentials"]
        assert body["personalAccessTokenName"] == "alice-pat"
        assert body["personalAccessTokenSecret"] == "secret"
        calls["signin"] += 1
        return httpx.Response(200, json={"credentials": {
            "token": "TAB-TOKEN", "site": {"id": "site-1"}, "user": {"id": "user-1"}}})

    cache = _signin_cache(handler)
    assert await cache.get_token("alice@corp", "alice-pat", "secret") == "TAB-TOKEN"
    # Повторный вызов — из кэша, второго signin нет (иначе убили бы сессию).
    assert await cache.get_token("alice@corp", "alice-pat", "secret") == "TAB-TOKEN"
    assert calls["signin"] == 1
    await cache.aclose()


async def test_concurrent_get_token_signs_in_once() -> None:
    """Ключевой инвариант: параллельные запросы одного юзера → ОДИН signin."""
    calls = {"signin": 0}

    async def slow_handler(req: httpx.Request) -> httpx.Response:
        calls["signin"] += 1
        await asyncio.sleep(0.05)  # окно для гонки
        return httpx.Response(200, json={"credentials": {
            "token": "T", "site": {"id": "s"}, "user": {"id": "u"}}})

    cache = _signin_cache(slow_handler)
    tokens = await asyncio.gather(*(cache.get_token("alice@corp", "p", "s") for _ in range(5)))
    assert tokens == ["T"] * 5
    assert calls["signin"] == 1  # лок сработал
    await cache.aclose()


async def test_invalidate_forces_new_signin() -> None:
    calls = {"signin": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["signin"] += 1
        return httpx.Response(200, json={"credentials": {
            "token": f"T{calls['signin']}", "site": {"id": "s"}, "user": {"id": "u"}}})

    cache = _signin_cache(handler)
    assert await cache.get_token("a", "p", "s") == "T1"
    cache.invalidate("a")
    assert await cache.get_token("a", "p", "s") == "T2"
    await cache.aclose()


def test_ssl_verify_from_env(monkeypatch) -> None:
    # Дефолт — строгая проверка (Cloud / публичный CA).
    monkeypatch.delenv("TABLEAU_SSL_VERIFY", raising=False)
    assert ta._verify_from_env() is True
    # Отключение — для доверенной сети.
    monkeypatch.setenv("TABLEAU_SSL_VERIFY", "false")
    assert ta._verify_from_env() is False
    # Путь к CA-bundle — корректный on-prem вариант для Tableau Server.
    monkeypatch.setenv("TABLEAU_SSL_VERIFY", "/certs/tableau-ca.pem")
    assert ta._verify_from_env() == "/certs/tableau-ca.pem"


async def test_signin_error_surfaces() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"summary": "bad token"}})

    cache = _signin_cache(handler)
    with pytest.raises(TableauSignInError):
        await cache.get_token("a", "p", "s")
    await cache.aclose()


# ─────────────────────────── broker (end-to-end через TestClient) ───────────────────────────
# Upstream — НАСТОЯЩЕЕ ASGI-приложение (не MockTransport): проверяем реальный
# стриминг ответа через брокер, как с живым tableau-mcp.
def _make_broker_app(store: MappingStore, upstream_app, signin_handler,
                     monkeypatch, *, verify=False) -> TestClient:
    monkeypatch.setenv("IDENTITY_VERIFY", "false" if verify is False else "true")
    monkeypatch.setenv("IDENTITY_CLAIM", "sub")
    upstream_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream_app))
    sessions = _signin_cache(signin_handler)
    b = bm.Broker(store, sessions, "http://upstream.local/tableau-mcp", upstream_client)
    app = Starlette(routes=[
        Route("/healthz", bm._health, methods=["GET"]),
        Route("/tableau-mcp", bm._handle, methods=["GET", "POST", "DELETE"]),
    ])
    app.state.broker = b
    return TestClient(app)


def _upstream_app(handler) -> Starlette:
    """Обернуть питон-хендлер (Request -> JSONResponse) в ASGI-приложение."""
    from starlette.responses import JSONResponse as _J

    async def _route(request: Request):
        return await handler(request) if asyncio.iscoroutinefunction(handler) else handler(request)

    return Starlette(routes=[Route("/tableau-mcp", _route, methods=["GET", "POST", "DELETE"])])


def _bearer_unverified(sub: str) -> dict[str, str]:
    return {"authorization": "Bearer " + jwt.encode({"sub": sub}, "x", algorithm="HS256")}


def test_broker_injects_tableau_auth_for_mapped_user(store: MappingStore, monkeypatch) -> None:
    store.put("alice@corp", "alice@corp", "alice-pat", "secret")
    seen = {}

    def upstream(request: Request) -> JSONResponse:
        seen["x-tableau-auth"] = request.headers.get("x-tableau-auth")
        return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    def signin(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credentials": {
            "token": "SESSION-XYZ", "site": {"id": "s"}, "user": {"id": "u"}}})

    client = _make_broker_app(store, _upstream_app(upstream), signin, monkeypatch)
    r = client.post("/tableau-mcp",
                    headers=_bearer_unverified("alice@corp"),
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "list-views"}})
    assert r.status_code == 200
    assert r.json()["result"] == {"ok": True}
    assert seen["x-tableau-auth"] == "SESSION-XYZ"  # брокер подставил session-token


def test_broker_blocks_unmapped_user_toolcall(store: MappingStore, monkeypatch) -> None:
    called = {"upstream": False}

    def upstream(request: Request) -> JSONResponse:
        called["upstream"] = True
        return JSONResponse({})

    def signin(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credentials": {"token": "t", "site": {"id": "s"}, "user": {"id": "u"}}})

    client = _make_broker_app(store, _upstream_app(upstream), signin, monkeypatch)
    r = client.post("/tableau-mcp",
                    headers=_bearer_unverified("nobody@corp"),
                    json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {}})
    assert r.status_code == 200
    err = r.json()["error"]
    assert err["code"] == -32001 and "nobody@corp" in err["message"]
    assert called["upstream"] is False  # до upstream/общего PAT не дошли


def test_broker_blocks_toolcall_without_identity(store: MappingStore, monkeypatch) -> None:
    def upstream(request): return JSONResponse({})
    def signin(req): return httpx.Response(200, json={"credentials": {"token": "t", "site": {"id": "s"}, "user": {"id": "u"}}})
    client = _make_broker_app(store, _upstream_app(upstream), signin, monkeypatch)
    r = client.post("/tableau-mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}})
    assert r.json()["error"]["code"] == -32001


def test_broker_passes_handshake_without_auth(store: MappingStore, monkeypatch) -> None:
    """tools/list и initialize идут в upstream БЕЗ x-tableau-auth (регистрация федерации)."""
    seen = {}

    def upstream(request: Request) -> JSONResponse:
        seen["auth"] = request.headers.get("x-tableau-auth")
        return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})

    def signin(req):  # не должен вызваться
        raise AssertionError("signin не нужен для tools/list")

    client = _make_broker_app(store, _upstream_app(upstream), signin, monkeypatch)
    r = client.post("/tableau-mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200
    assert seen["auth"] is None


def test_broker_refreshes_session_on_upstream_401(store: MappingStore, monkeypatch) -> None:
    store.put("alice@corp", "alice@corp", "alice-pat", "secret")
    state = {"upstream": 0, "signin": 0}

    def upstream(request: Request) -> JSONResponse:
        state["upstream"] += 1
        if state["upstream"] == 1:
            return JSONResponse({"error": "invalid_token"}, status_code=401)  # протухшая сессия
        return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    def signin(req: httpx.Request) -> httpx.Response:
        state["signin"] += 1
        return httpx.Response(200, json={"credentials": {
            "token": f"S{state['signin']}", "site": {"id": "s"}, "user": {"id": "u"}}})

    client = _make_broker_app(store, _upstream_app(upstream), signin, monkeypatch)
    r = client.post("/tableau-mcp",
                    headers=_bearer_unverified("alice@corp"),
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
    assert r.status_code == 200
    assert r.json()["result"] == {"ok": True}
    assert state["upstream"] == 2 and state["signin"] == 2  # инвалидация + релогин + retry


# ─────────────────────── отказоустойчивость ───────────────────────
def test_broker_upstream_unavailable_returns_clean_error(store: MappingStore, monkeypatch, caplog) -> None:
    """tableau-mcp недоступен → чистая JSON-RPC ошибка (не 500/креш), и аудит
    пишет РЕАЛЬНЫЙ исход (error/upstream), а не ложный успешный call."""
    store.put("alice@corp", "alice@corp", "alice-pat", "secret")
    monkeypatch.setenv("IDENTITY_VERIFY", "false")

    def raiser(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream down")

    def signin(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credentials": {"token": "S", "site": {"id": "s"}, "user": {"id": "u"}}})

    up_client = httpx.AsyncClient(transport=httpx.MockTransport(raiser))
    b = bm.Broker(store, _signin_cache(signin), "http://up.local/tableau-mcp", up_client)
    app = Starlette(routes=[Route("/tableau-mcp", bm._handle, methods=["POST"])])
    app.state.broker = b
    with caplog.at_level(logging.INFO, logger="tableau_identity.audit"):
        r = TestClient(app).post("/tableau-mcp", headers=_bearer_unverified("alice@corp"),
                                 json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list-views"}})
    assert r.json()["error"]["code"] == -32003
    a = [json.loads(x.message) for x in caplog.records if x.name == "tableau_identity.audit"]
    assert any(e["event"] == "error" and e["reason"] == "upstream" and e["tool"] == "list-views" for e in a)
    assert not any(e["event"] == "call" for e in a)  # ложного успешного call быть не должно


def test_store_serves_stale_mapping_on_db_error(tmp_path, monkeypatch) -> None:
    """Если Postgres икнул, брокер получает ПОСЛЕДНЮЮ удачную копию, а не исключение."""
    from tableau_identity import store as st
    key = generate_key()
    enc = st.Fernet(key.encode()).encrypt(b"secret").decode()
    row = {"alice@corp": {"user": "alice@corp", "tableau_username": "a@t", "pat_name": "p", "pat_secret_enc": enc}}
    state = {"n": 0}

    def read_all(_url):
        state["n"] += 1
        if state["n"] == 1:
            return row
        raise RuntimeError("db down")  # второй раз — БД недоступна

    monkeypatch.setattr(st._pg, "init", lambda url: None)
    monkeypatch.setattr(st._pg, "read_all", read_all)
    monkeypatch.setenv("IDENTITY_DB_TTL_SECONDS", "0")  # перечитывать на каждый get
    s = st.MappingStore(tmp_path / "unused.yml", key, db_url="postgresql://dummy")

    assert s.get("alice@corp").pat_secret == "secret"      # первая загрузка ок
    assert s.get("alice@corp").pat_secret == "secret"      # БД упала → отдаём кэш, не падаем


# ─────────────────────── аудит: учётка ↔ инструмент ───────────────────────
def _audits(caplog) -> list[dict]:
    return [json.loads(r.message) for r in caplog.records if r.name == "tableau_identity.audit"]


def test_broker_audits_toolcall_user_and_tool(store: MappingStore, monkeypatch, caplog) -> None:
    store.put("alice@corp", "alice@corp", "alice-pat", "secret")

    def upstream(request: Request) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    def signin(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credentials": {"token": "S", "site": {"id": "s"}, "user": {"id": "u"}}})

    client = _make_broker_app(store, _upstream_app(upstream), signin, monkeypatch)
    with caplog.at_level(logging.INFO, logger="tableau_identity.audit"):
        client.post("/tableau-mcp", headers=_bearer_unverified("alice@corp"),
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list-views"}})
    a = _audits(caplog)
    assert any(x["event"] == "call" and x["user"] == "alice@corp" and x["tool"] == "list-views"
               and x["tableau_user"] == "alice@corp" and "ts" in x for x in a)


def test_broker_audits_blocked_unmapped(store: MappingStore, monkeypatch, caplog) -> None:
    def upstream(request: Request) -> JSONResponse:
        return JSONResponse({})

    def signin(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credentials": {"token": "t", "site": {"id": "s"}, "user": {"id": "u"}}})

    client = _make_broker_app(store, _upstream_app(upstream), signin, monkeypatch)
    with caplog.at_level(logging.INFO, logger="tableau_identity.audit"):
        client.post("/tableau-mcp", headers=_bearer_unverified("nobody@corp"),
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "query-datasource"}})
    a = _audits(caplog)
    assert any(x["event"] == "blocked" and x["reason"] == "no_mapping" and x["tool"] == "query-datasource" for x in a)


def test_broker_audits_tool_error_result(store: MappingStore, monkeypatch, caplog) -> None:
    """Инструмент вернул isError=true (HTTP 200) → аудит помечает tool_error, а не «успех»."""
    store.put("alice@corp", "alice@corp", "alice-pat", "secret")

    def upstream(request: Request) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {"isError": True,
                             "content": [{"type": "text", "text": "boom"}]}})

    def signin(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credentials": {"token": "S", "site": {"id": "s"}, "user": {"id": "u"}}})

    client = _make_broker_app(store, _upstream_app(upstream), signin, monkeypatch)
    with caplog.at_level(logging.INFO, logger="tableau_identity.audit"):
        client.post("/tableau-mcp", headers=_bearer_unverified("alice@corp"),
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "query-datasource"}})
    a = _audits(caplog)
    assert any(x["event"] == "call" and x.get("tool_error") is True and x["tool"] == "query-datasource" for x in a)


# ─────────────────────────── admin ───────────────────────────
@pytest.fixture
def admin_client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("MAPPINGS_PATH", str(tmp_path / "mappings.yml"))
    monkeypatch.setenv("TABLEAU_IDENTITY_ENC_KEY", generate_key())
    monkeypatch.setenv("IDENTITY_ADMIN_USER", "admin")
    monkeypatch.setenv("IDENTITY_ADMIN_PASSWORD", "secret")
    return TestClient(admin.build_app())


def _auth() -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}


def test_admin_requires_auth(admin_client: TestClient) -> None:
    assert admin_client.get("/").status_code == 401
    assert admin_client.get("/", headers=_auth()).status_code == 200
    assert admin_client.get("/healthz").status_code == 200  # health открыт


def test_admin_upsert_and_api_hides_secret(admin_client: TestClient) -> None:
    r = admin_client.post("/", headers=_auth(), data={
        "user": "alice@corp", "tableau_username": "alice@corp",
        "pat_name": "alice-pat", "pat_secret": "supersecret"},
        follow_redirects=False)
    assert r.status_code == 303
    api = admin_client.get("/api/mappings", headers=_auth()).json()
    assert api["mappings"][0]["user"] == "alice@corp"
    assert "supersecret" not in json.dumps(api)  # секрет наружу не отдаётся


def test_admin_delete(admin_client: TestClient) -> None:
    admin_client.post("/", headers=_auth(), data={
        "user": "bob@corp", "tableau_username": "bob@corp", "pat_name": "p", "pat_secret": "s"})
    admin_client.post("/delete", headers=_auth(), data={"user": "bob@corp"}, follow_redirects=False)
    assert admin_client.get("/api/mappings", headers=_auth()).json()["mappings"] == []


def test_admin_rejects_duplicate_tableau_account(admin_client: TestClient) -> None:
    admin_client.post("/", headers=_auth(), data={
        "user": "a@corp", "tableau_username": "shared", "pat_name": "pa", "pat_secret": "sa"})
    r = admin_client.post("/", headers=_auth(), data={
        "user": "b@corp", "tableau_username": "shared", "pat_name": "pb", "pat_secret": "sb"})
    assert r.status_code == 400  # инвариант 1:1


# ─────────────────────── шов с НАСТОЯЩИМ гейтом ───────────────────────
# Опциональный интеграционный тест: прогоняет реальную функцию проброса
# заголовков из исходников ContextForge против реального кода брокера. Без
# гейт-стека — только импорт функции. Пропускается, если исходников/зависимостей
# гейта в окружении нет (обычный CI), выполняется там, где они установлены.
def test_seam_real_gateway_forwards_identity_to_broker(monkeypatch) -> None:
    import sys
    from pathlib import Path

    gw = Path(__file__).resolve().parent.parent.parent / "mcp-context-forge"
    if not gw.exists():
        pytest.skip("исходники ContextForge не найдены")
    sys.path.insert(0, str(gw))
    ph = pytest.importorskip(
        "mcpgateway.utils.passthrough_headers",
        reason="зависимости гейта не установлены",
    )

    secret = "seam-secret-at-least-32-bytes-xxxxxx"
    user_token = "Bearer " + jwt.encode({"sub": "alice@corp"}, secret, algorithm="HS256")

    # Клиент дублирует свой токен в X-Upstream-Authorization; у гейта свой auth к upstream.
    upstream = ph.compute_passthrough_headers_cached(
        {"authorization": "Bearer GATEWAY-LOGIN", "x-upstream-authorization": user_token},
        {"Authorization": "Bearer GATEWAY-OWN"},
        [],
        gateway_auth_type="bearer",
        gateway_passthrough_headers=None,
    )
    # Реальная функция гейта перекрыла свой auth личностью юзера.
    assert upstream.get("Authorization") == user_token

    # Реальный брокер достаёт из этого sub пользователя.
    cfg = IdentityConfig(header="authorization", secret=secret, algorithms=["HS256"],
                         claim="sub", verify=True, audience=None, issuer=None)
    assert extract_user({k.lower(): v for k, v in upstream.items()}, cfg) == "alice@corp"

    # Контроль: без X-Upstream-Authorization личность юзера в upstream не попадает.
    ctrl = ph.compute_passthrough_headers_cached(
        {"authorization": "Bearer GATEWAY-LOGIN"}, {"Authorization": "Bearer GATEWAY-OWN"},
        [], gateway_auth_type="bearer",
    )
    assert ctrl.get("Authorization") == "Bearer GATEWAY-OWN"
