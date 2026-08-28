"""Определение ИИ-пользователя из запроса, пришедшего через гейт.

Клиент дублирует свой gateway-JWT в `X-Upstream-Authorization`; ContextForge
переименовывает его в `Authorization` для upstream (см. passthrough_headers).
Брокер валидирует подпись тем же секретом, что и гейт (defense-in-depth на
внутренней сети), и берёт из токена claim личности (по умолчанию `sub`).

OAuth встроенного провайдера ContextForge — HS256 c JWT_SECRET_KEY. Внешний IdP
(Keycloak, RS256/JWKS) — по ТС «архитектурная возможность»; сюда добавляется
отдельной веткой проверки, текущая реализация закрывает встроенный провайдер.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import jwt


@dataclass(frozen=True)
class IdentityConfig:
    header: str
    secret: str
    algorithm: str
    claim: str
    verify: bool
    audience: str | None
    issuer: str | None

    @classmethod
    def from_env(cls) -> "IdentityConfig":
        return cls(
            header=os.environ.get("IDENTITY_HEADER", "authorization").lower(),
            secret=os.environ.get("IDENTITY_JWT_SECRET") or os.environ.get("JWT_SECRET_KEY", ""),
            algorithm=os.environ.get("IDENTITY_JWT_ALGORITHM")
            or os.environ.get("JWT_ALGORITHM", "HS256"),
            claim=os.environ.get("IDENTITY_CLAIM", "sub"),
            verify=os.environ.get("IDENTITY_VERIFY", "true").lower() != "false",
            audience=os.environ.get("IDENTITY_JWT_AUDIENCE") or None,
            issuer=os.environ.get("IDENTITY_JWT_ISSUER") or None,
        )


def _bearer(raw: str) -> str | None:
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def extract_user(headers: dict[str, str], cfg: IdentityConfig) -> str | None:
    """Вернуть ключ пользователя (claim) или None, если токена/личности нет.

    None — это не ошибка: MCP-хендшейк и tools/list гейт зовёт без пользователя,
    их надо пропускать. Гейтинг tool-вызовов делает брокер.
    """
    raw = headers.get(cfg.header, "")
    token = _bearer(raw) if cfg.header == "authorization" else (raw.strip() or None)
    if not token:
        return None
    try:
        if cfg.verify:
            if not cfg.secret:
                raise jwt.InvalidTokenError("IDENTITY_JWT_SECRET/JWT_SECRET_KEY не задан")
            claims = jwt.decode(
                token,
                cfg.secret,
                algorithms=[cfg.algorithm],
                audience=cfg.audience,
                issuer=cfg.issuer,
                options={"verify_aud": cfg.audience is not None},
            )
        else:
            claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return None
    value = claims.get(cfg.claim)
    return str(value) if value else None
