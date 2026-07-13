"""Тесты для bootstrap-скриптов: минтинг JWT и регистрация MCP-серверов."""
from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
import pytest

import register
import mint_token


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
