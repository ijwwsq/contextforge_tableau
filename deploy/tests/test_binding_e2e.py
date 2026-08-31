"""Максимально детальный e2e связки «юзер ↔ PAT» — БЕЗ поднятия стека.

Вся цепочка привязки прогоняется на РЕАЛЬНОМ коде каждого звена. Внешние системы
(Tableau, tableau-mcp) — верные фейки, которые соблюдают их контракт; гейт —
его НАСТОЯЩАЯ функция проброса заголовков из исходников ContextForge.

Звенья цепочки:
  1) токен как выдаёт гейт (личный API-токен: sub=email, HS256, JWT_SECRET_KEY)
  2) ГЕЙТ (реальная compute_passthrough_headers_cached): X-Upstream-Authorization → Authorization
  3) identity.extract_user: проверка подписи → sub
  4) MappingStore (шифрованный на диске): sub → {tableau_username, pat_name, pat_secret}
  5) SessionCache: PAT → session-token (правило «один PAT = одна сессия», пер-юзерный лок)
  6) Broker: инъекция x-tableau-auth → tableau-mcp ходит под сессией ЭТОГО юзера (RLS)
  7) авто-релогин на 401, гейтинг непривязанных, отказ поддельной подписи, аудит.

Фейк Tableau связан с фейком tableau-mcp общим реестром сессий: signin выдаёт
токен, привязанный к учётке Tableau; tableau-mcp по этому токену отвечает, ПОД
КАКОЙ учёткой пошёл вызов — так проверяем, что RLS реально «под юзером».
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import httpx
import jwt
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tableau_identity import broker as bm
from tableau_identity.store import MappingStore, generate_key
from tableau_identity.tableau_auth import SessionCache, TableauEndpoint

SECRET = "gw-jwt-secret-at-least-32-bytes-long-xx"


# ───────────────────────── фейковый мир Tableau ─────────────────────────
class TableauWorld:
    """Верный фейк Tableau + tableau-mcp, связанные общим реестром сессий."""

    def __init__(self) -> None:
        self.pats: dict[tuple[str, str], str] = {}   # (name, secret) -> tableau_user
        self.sessions: dict[str, str] = {}           # token -> tableau_user (валидные)
        self.signins: dict[str, int] = {}            # pat_name -> сколько раз логинились
        self._n = 0

    def register_pat(self, name: str, secret: str, tableau_user: str) -> None:
        self.pats[(name, secret)] = tableau_user

    def signin(self, name: str, secret: str) -> str | None:
        tu = self.pats.get((name, secret))
        if tu is None:
            return None                              # невалидный PAT
        self.signins[name] = self.signins.get(name, 0) + 1
        # Правило Tableau: новый signin тем же PAT убивает прошлую сессию этого юзера.
        for tok, u in list(self.sessions.items()):
            if u == tu:
                del self.sessions[tok]
        self._n += 1
        token = f"SESS-{tu}-{self._n}"
        self.sessions[token] = tu
        return token

    def whoami(self, token: str | None) -> str | None:
        return self.sessions.get(token or "")

    def revoke(self, token: str) -> None:
        self.sessions.pop(token, None)


def _tableau_signin_cache(world: TableauWorld) -> SessionCache:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/auth/signin")
        creds = json.loads(req.content)["credentials"]
        tok = world.signin(creds["personalAccessTokenName"], creds["personalAccessTokenSecret"])
        if tok is None:
            return httpx.Response(401, json={"error": {"summary": "Signin Error",
                                  "detail": "The personal access token you provided is invalid."}})
        return httpx.Response(200, json={"credentials": {"token": tok, "site": {"id": "s"}, "user": {"id": "u"}}})

    client = httpx.AsyncClient(base_url="https://tab.example.com/api/3.22",
                               transport=httpx.MockTransport(handler),
                               headers={"Accept": "application/json"})
    ep = TableauEndpoint(server="https://tab.example.com", site_content_url="", api_version="3.22")
    return SessionCache(ep, client=client, ttl_seconds=10_000)


def _fake_tableau_mcp(world: TableauWorld) -> Starlette:
    async def handle(request: Request) -> JSONResponse:
        body = json.loads(await request.body())
        if body.get("method") != "tools/call":
            return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": []}})
        tu = world.whoami(request.headers.get("x-tableau-auth"))
        if tu is None:
            return JSONResponse({"error": "invalid_token"}, status_code=401)  # протухшая/чужая сессия
        # tableau-mcp «ходит» под учёткой юзера — возвращаем, под кем именно (RLS).
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": {"acting_as": tu}})

    return Starlette(routes=[Route("/tableau-mcp", handle, methods=["GET", "POST", "DELETE"])])


# ───────────────────────── собранная цепочка ─────────────────────────
@pytest.fixture
def chain(tmp_path, monkeypatch):
    """Реальные store+sessions+broker, подключённые к фейковому миру Tableau."""
    monkeypatch.setenv("IDENTITY_VERIFY", "true")
    monkeypatch.setenv("IDENTITY_JWT_SECRET", SECRET)
    monkeypatch.setenv("IDENTITY_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("IDENTITY_CLAIM", "sub")

    world = TableauWorld()
    world.register_pat("alice-pat", "alice-secret", "alice@tableau")
    world.register_pat("bob-pat", "bob-secret", "bob@tableau")

    store = MappingStore(tmp_path / "mappings.yml", generate_key())
    store.put("alice@corp", "alice@tableau", "alice-pat", "alice-secret")
    store.put("bob@corp", "bob@tableau", "bob-pat", "bob-secret")

    sessions = _tableau_signin_cache(world)
    upstream_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=_fake_tableau_mcp(world)))
    broker = bm.Broker(store, sessions, "http://mcp.local/tableau-mcp", upstream_client)

    app = Starlette(routes=[Route("/tableau-mcp", bm._handle, methods=["GET", "POST", "DELETE"])])
    app.state.broker = broker
    client = TestClient(app)
    return type("Chain", (), {"world": world, "store": store, "client": client, "mappings_path": tmp_path / "mappings.yml"})


def _gateway_token(sub: str, secret: str = SECRET) -> str:
    """Точная форма личного API-токена гейта: sub=email, token_use=api, HS256."""
    return jwt.encode({"sub": sub, "email": sub, "token_use": "api"}, secret, algorithm="HS256")


def _forward_through_gateway(user_token: str) -> dict[str, str]:
    """Клиент кладёт токен в X-Upstream-Authorization; гейт (РЕАЛЬНАЯ функция)
    переименовывает его в Authorization для upstream. Возвращает заголовки для брокера."""
    gw = Path(__file__).resolve().parent.parent.parent / "mcp-context-forge"
    if not gw.exists():
        pytest.skip("исходники ContextForge не найдены")
    sys.path.insert(0, str(gw))
    ph = pytest.importorskip("mcpgateway.utils.passthrough_headers", reason="зависимости гейта не установлены")
    out = ph.compute_passthrough_headers_cached(
        {"authorization": "Bearer GATEWAY-LOGIN", "x-upstream-authorization": f"Bearer {user_token}"},
        {"Authorization": "Bearer GATEWAY-OWN"}, [],
        gateway_auth_type="bearer", gateway_passthrough_headers=None,
    )
    return {k: v for k, v in out.items()}


def _toolcall(client: TestClient, headers: dict[str, str], tool: str = "list-views", id_: int = 1):
    return client.post("/tableau-mcp", headers=headers,
                       json={"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": {"name": tool}})


# ═══════════════════════════ ЗВЕНЬЯ ЦЕПОЧКИ ═══════════════════════════

def test_link4_secret_encrypted_at_rest(chain) -> None:
    """Звено 4: PAT-секрет на диске — только шифртекст, в открытую его нет."""
    on_disk = chain.mappings_path.read_text(encoding="utf-8")
    assert "alice-secret" not in on_disk and "bob-secret" not in on_disk
    assert "pat_secret_enc" in on_disk


def test_full_chain_alice_goes_to_tableau_as_herself(chain) -> None:
    """Звенья 1→6: токен alice → гейт → брокер → её PAT → её сессия →
    tableau-mcp отвечает, что ходит ПОД alice@tableau (RLS под юзером)."""
    headers = _forward_through_gateway(_gateway_token("alice@corp"))
    # Звено 2 — реальная функция гейта реально положила Authorization из X-Upstream.
    assert headers["Authorization"] == f"Bearer {_gateway_token('alice@corp')}"
    r = _toolcall(chain.client, headers)
    assert r.status_code == 200
    assert r.json()["result"]["acting_as"] == "alice@tableau"


def test_per_user_isolation_bob_is_bob(chain) -> None:
    """Изоляция: bob под своим токеном ходит как bob@tableau, не как alice."""
    r = _toolcall(chain.client, _forward_through_gateway(_gateway_token("bob@corp")))
    assert r.json()["result"]["acting_as"] == "bob@tableau"


def test_link5_single_session_one_signin_across_calls(chain) -> None:
    """Звено 5: правило «один PAT = одна сессия» — сессия переиспользуется,
    на несколько вызовов alice приходится ОДИН signin (иначе убивали бы сессии)."""
    h = _forward_through_gateway(_gateway_token("alice@corp"))
    for i in range(3):
        assert _toolcall(chain.client, h, id_=i).json()["result"]["acting_as"] == "alice@tableau"
    assert chain.world.signins["alice-pat"] == 1


def test_link6_expired_session_auto_relogin_and_retry(chain) -> None:
    """Звено 6/авто-релогин: если сессия протухла (Tableau отозвал токен),
    брокер ловит 401 от tableau-mcp, логинится заново и повторяет — прозрачно."""
    h = _forward_through_gateway(_gateway_token("alice@corp"))
    assert _toolcall(chain.client, h).json()["result"]["acting_as"] == "alice@tableau"
    before = chain.world.signins["alice-pat"]
    # Отзываем текущую сессию alice (эмулируем idle-протухание).
    for tok, u in list(chain.world.sessions.items()):
        if u == "alice@tableau":
            chain.world.revoke(tok)
    r = _toolcall(chain.client, h, id_=99)
    assert r.status_code == 200 and r.json()["result"]["acting_as"] == "alice@tableau"
    assert chain.world.signins["alice-pat"] == before + 1  # был релогин


def test_unmapped_user_blocked_before_tableau(chain) -> None:
    """Непривязанный юзер: чистая ошибка, до Tableau/общего PAT запрос НЕ доходит."""
    signins_before = dict(chain.world.signins)
    r = _toolcall(chain.client, _forward_through_gateway(_gateway_token("stranger@corp")))
    err = r.json()["error"]
    assert err["code"] == -32001 and "stranger@corp" in err["message"]
    assert chain.world.signins == signins_before  # ни одного signin не случилось


def test_forged_token_rejected_by_broker(chain) -> None:
    """Поддельная подпись: гейт пробрасывает как есть (он не наш валидатор),
    но брокер проверяет подпись своим секретом → личности нет → tool-вызов режется."""
    forged = _gateway_token("alice@corp", secret="WRONG-SECRET")
    r = _toolcall(chain.client, _forward_through_gateway(forged))
    assert r.json()["error"]["code"] == -32001


def test_login_token_with_uuid_sub_would_not_match(chain) -> None:
    """Контроль соглашения: login-токен гейта несёт sub=UUID (не email) — по нему
    привязка НЕ находится. Поэтому в Claude Desktop идёт именно API-токен (sub=email)."""
    login_like = jwt.encode({"sub": "b1f7-uuid-not-email", "token_use": "session"}, SECRET, algorithm="HS256")
    r = _toolcall(chain.client, _forward_through_gateway(login_like))
    assert r.json()["error"]["code"] == -32001  # нет привязки на UUID


def test_audit_records_account_and_tool(chain, caplog) -> None:
    """Аудит: на успешный вызов пишется учётка ↔ инструмент ↔ учётка Tableau."""
    h = _forward_through_gateway(_gateway_token("alice@corp"))
    with caplog.at_level(logging.INFO, logger="tableau_identity.audit"):
        _toolcall(chain.client, h, tool="query-datasource")
    audits = [json.loads(r.message) for r in caplog.records if r.name == "tableau_identity.audit"]
    assert any(a["event"] == "call" and a["user"] == "alice@corp"
               and a["tool"] == "query-datasource" and a["tableau_user"] == "alice@tableau" for a in audits)
