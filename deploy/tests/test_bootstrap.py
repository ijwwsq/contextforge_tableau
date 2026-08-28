"""Тесты для bootstrap-скриптов: минтинг JWT и регистрация MCP-серверов."""
from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
import pytest

import register
import mint_token
import provision_user
import provision_roles


# ---------------------------------------------------------------- JWT


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("JWT_AUDIENCE", "aud")
    monkeypatch.setenv("JWT_ISSUER", "iss")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")


def test_mint_admin_token_has_admin_bypass_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    token = register.mint_admin_token(expiry_seconds=60)

    decoded = jwt.decode(token, "s" * 32, algorithms=["HS256"], audience="aud", issuer="iss")
    assert decoded["is_admin"] is True
    assert decoded["teams"] is None  # обязательный admin-bypass ContextForge
    assert decoded["sub"] == "admin@example.com"
    assert decoded["email"] == "admin@example.com"
    # Токен просуществует не меньше TTL, который мы попросили.
    assert decoded["exp"] - decoded["iat"] == 60


def test_mint_admin_token_respects_subject_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    token = register.mint_admin_token(expiry_seconds=60, subject="ci@example.com")
    decoded = jwt.decode(token, "s" * 32, algorithms=["HS256"], audience="aud", issuer="iss")
    assert decoded["sub"] == "ci@example.com"


def test_mint_token_script_reads_expiry_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("TOKEN_EXPIRY_SECONDS", "120")
    mint_token.main()
    out = capsys.readouterr().out.strip()
    decoded = jwt.decode(out, "s" * 32, algorithms=["HS256"], audience="aud", issuer="iss")
    assert decoded["exp"] - decoded["iat"] == 120


def test_mint_token_script_defaults_to_no_expiry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _base_env(monkeypatch)
    monkeypatch.delenv("TOKEN_EXPIRY_SECONDS", raising=False)
    mint_token.main()
    out = capsys.readouterr().out.strip()
    # options={"verify_exp": False}: exp-клейма быть не должно вовсе, но jwt.decode
    # без него всё равно валиден — проверяем по payload напрямую.
    decoded = jwt.decode(out, "s" * 32, algorithms=["HS256"], audience="aud", issuer="iss")
    assert "exp" not in decoded


def test_mint_user_token_is_not_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    token = register.mint_user_token("alice@corp")
    decoded = jwt.decode(token, "s" * 32, algorithms=["HS256"], audience="aud", issuer="iss")
    assert decoded["sub"] == "alice@corp"        # sub = личность для tableau-identity
    assert decoded["is_admin"] is False          # обычный пользователь, не admin-bypass
    assert "exp" not in decoded                  # по умолчанию бессрочный


def test_mint_admin_token_zero_expiry_has_no_exp(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    token = register.mint_admin_token(expiry_seconds=0)
    decoded = jwt.decode(token, "s" * 32, algorithms=["HS256"], audience="aud", issuer="iss")
    assert "exp" not in decoded
    assert decoded["is_admin"] is True  # bypass-клеймы никуда не делись


# ---------------------------------------------------------------- Провижининг юзера (1.1)


def test_provision_create_user_new_and_existing(capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/auth/email/admin/users"
        import json
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"email": "alice@corp"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        provision_user.create_user(c, "http://gw", {}, "alice@corp", "pw12345678")
    # Тело — реальные поля AdminCreateUserRequest, is_admin=False (обычный юзер).
    assert seen[0]["email"] == "alice@corp"
    assert seen[0]["is_admin"] is False

    def handler409(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "already exists"})

    with httpx.Client(transport=httpx.MockTransport(handler409)) as c:
        provision_user.create_user(c, "http://gw", {}, "alice@corp", "pw")  # не бросает


def test_provision_assign_role_resolves_id() -> None:
    calls: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/rbac/roles":
            return httpx.Response(200, json=[{"id": "r-1", "name": "developer"}, {"id": "r-2", "name": "viewer"}])
        if request.method == "POST" and request.url.path == "/rbac/users/alice@corp/roles":
            import json
            calls["body"] = json.loads(request.content)
            return httpx.Response(201, json={})
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        provision_user.assign_role(c, "http://gw", {}, "alice@corp", "developer")
    assert calls["body"]["role_id"] == "r-1"  # имя роли → её id
    assert calls["body"]["scope"] == "global"


def test_provision_assign_role_unknown_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "r-2", "name": "viewer"}])

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(SystemExit, match="не найдена"):
            provision_user.assign_role(c, "http://gw", {}, "alice@corp", "developer")


def test_provision_issue_token_returns_sub_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKEN_EXPIRES_IN_DAYS", raising=False)
    body_seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/tokens"
        import json
        body_seen.update(json.loads(request.content))
        return httpx.Response(201, json={"access_token": "PERSONAL-TOKEN"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        tok = provision_user.issue_token(c, "http://gw", {}, "alice@corp")
    assert tok == "PERSONAL-TOKEN"
    # admin-делегация: выпускаем ЗА пользователя, его email — в user_email.
    assert body_seen["user_email"] == "alice@corp"


# ---------------------------------------------------------------- Роли RBAC (1.2)


def test_roles_definitions_cover_tc_and_end_user_can_execute() -> None:
    names = {r["name"] for r in provision_roles.ROLES}
    assert names == {"mcp-platform-admin", "mcp-access-admin", "mcp-analyst", "mcp-user"}
    # Критично: конечный юзер МОЖЕТ звать инструменты, но НЕ может их портить.
    user = next(r for r in provision_roles.ROLES if r["name"] == "mcp-user")
    assert "tools.execute" in user["permissions"] and "servers.use" in user["permissions"]
    for destructive in ("tools.delete", "tools.create", "servers.delete", "gateways.create"):
        assert destructive not in user["permissions"], f"{destructive} не должно быть у mcp-user"


def test_provision_roles_creates_missing_skips_existing(capsys: pytest.CaptureFixture[str]) -> None:
    created: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/rbac/roles":
            return httpx.Response(200, json=[{"name": "mcp-user"}])  # одна уже есть
        if request.method == "POST" and request.url.path == "/rbac/roles":
            import json
            created.append(json.loads(request.content)["name"])
            return httpx.Response(201, json={})
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        h = {"Authorization": "Bearer x"}
        have = provision_roles.existing_role_names(c, "http://gw", h)
        for role in provision_roles.ROLES:
            if role["name"] not in have:
                provision_roles.create_role(c, "http://gw", h, role)
    # mcp-user пропущена, остальные три созданы.
    assert "mcp-user" not in created
    assert set(created) == {"mcp-platform-admin", "mcp-access-admin", "mcp-analyst"}


# ---------------------------------------------------------------- Регистрация


def _stub_client(responses: dict[tuple[str, str], httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key in responses:
            resp = responses[key]
            if isinstance(resp, list):
                return resp.pop(0)
            return resp
        raise AssertionError(f"unexpected request: {key}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_register_gateway_creates_new_entry(capsys: pytest.CaptureFixture[str]) -> None:
    responses = {("POST", "/gateways"): httpx.Response(201, json={})}
    with _stub_client(responses) as client:
        register.register_gateway(client, "http://gw", {}, {"name": "x", "url": "http://x", "transport": "STREAMABLEHTTP"})
    assert "registered x" in capsys.readouterr().out


def test_register_gateway_updates_on_conflict(capsys: pytest.CaptureFixture[str]) -> None:
    responses = {
        ("POST", "/gateways"): httpx.Response(409, json={}),
        ("GET", "/gateways"): httpx.Response(200, json=[{"id": "42", "name": "x"}]),
        ("PUT", "/gateways/42"): httpx.Response(200, json={}),
    }
    with _stub_client(responses) as client:
        register.register_gateway(client, "http://gw", {}, {"name": "x", "url": "http://x", "transport": "STREAMABLEHTTP"})
    assert "updated x" in capsys.readouterr().out


def test_register_gateway_raises_when_conflict_but_missing_in_listing() -> None:
    responses = {
        ("POST", "/gateways"): httpx.Response(409, json={}),
        ("GET", "/gateways"): httpx.Response(200, json=[]),
    }
    with _stub_client(responses) as client:
        with pytest.raises(SystemExit, match="not found in listing"):
            register.register_gateway(client, "http://gw", {}, {"name": "x", "url": "http://x", "transport": "STREAMABLEHTTP"})


def test_register_gateway_raises_on_other_errors() -> None:
    responses = {("POST", "/gateways"): httpx.Response(500, text="nope")}
    with _stub_client(responses) as client:
        with pytest.raises(SystemExit, match="failed to register"):
            register.register_gateway(client, "http://gw", {}, {"name": "x", "url": "http://x", "transport": "STREAMABLEHTTP"})


def test_wait_for_gateway_returns_when_healthy() -> None:
    responses = {("GET", "/health"): httpx.Response(200, text="ok")}
    with _stub_client(responses) as client:
        register.wait_for_gateway(client, "http://gw", timeout=1.0)  # не должно бросить


def test_wait_for_gateway_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    # Уменьшаем шаг ожидания, чтобы тест не подвисал на 2s sleep.
    monkeypatch.setattr(register.time, "sleep", lambda _s: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SystemExit, match="not reachable"):
            register.wait_for_gateway(client, "http://gw", timeout=0.05)
