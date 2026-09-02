"""Определение ИИ-пользователя из запроса, пришедшего через гейт.

Клиент дублирует свой gateway-JWT в `X-Upstream-Authorization`; ContextForge
переименовывает его в `Authorization` для upstream (см. passthrough_headers).
Брокер валидирует подпись и берёт claim личности (по умолчанию `sub`).

Поддерживаются три способа проверки подписи (чтобы не зависеть от конфига гейта):
- **HS*** (дефолт): общий секрет `IDENTITY_JWT_SECRET`/`JWT_SECRET_KEY` — встроенный
  провайдер ContextForge (HS256).
- **RS*/ES*/PS*** со статическим публичным ключом: `IDENTITY_JWT_PUBLIC_KEY` (PEM)
  или `IDENTITY_JWT_PUBLIC_KEY_PATH` — если гейт настроен на асимметричную подпись.
- **JWKS** (Keycloak/OIDC): `IDENTITY_JWKS_URL` — ключи тянутся по `kid` и кэшируются.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import jwt

# Кэш JWKS-клиентов по URL (тянут ключи и кэшируют их внутри себя).
_jwk_clients: dict[str, "jwt.PyJWKClient"] = {}


def _asymmetric(algorithms: list[str]) -> bool:
    return any(a.upper().startswith(("RS", "ES", "PS", "EDDSA")) for a in algorithms)


@dataclass(frozen=True)
class IdentityConfig:
    header: str
    claim: str
    verify: bool
    audience: str | None
    issuer: str | None
    algorithms: list[str] = field(default_factory=lambda: ["HS256"])
    secret: str = ""          # для HS*
    public_key: str = ""      # PEM для RS*/ES*/PS* (статический)
    jwks_url: str = ""        # OIDC JWKS (Keycloak) — динамические ключи

    @classmethod
    def from_env(cls) -> "IdentityConfig":
        algs = [a.strip() for a in (
            os.environ.get("IDENTITY_JWT_ALGORITHM") or os.environ.get("JWT_ALGORITHM", "HS256")
        ).split(",") if a.strip()]
        pub = os.environ.get("IDENTITY_JWT_PUBLIC_KEY", "")
        path = os.environ.get("IDENTITY_JWT_PUBLIC_KEY_PATH", "")
        if not pub and path and os.path.exists(path):
            pub = open(path, encoding="utf-8").read()
        return cls(
            header=os.environ.get("IDENTITY_HEADER", "authorization").lower(),
            claim=os.environ.get("IDENTITY_CLAIM", "sub"),
            verify=os.environ.get("IDENTITY_VERIFY", "true").lower() != "false",
            audience=os.environ.get("IDENTITY_JWT_AUDIENCE") or None,
            issuer=os.environ.get("IDENTITY_JWT_ISSUER") or None,
            algorithms=algs or ["HS256"],
            secret=os.environ.get("IDENTITY_JWT_SECRET") or os.environ.get("JWT_SECRET_KEY", ""),
            public_key=pub,
            jwks_url=os.environ.get("IDENTITY_JWKS_URL", ""),
        )


def _bearer(raw: str) -> str | None:
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _signing_key(token: str, cfg: IdentityConfig):
    """Ключ для проверки подписи: JWKS → публичный ключ → секрет (HS)."""
    if cfg.jwks_url:
        client = _jwk_clients.get(cfg.jwks_url)
        if client is None:
            client = jwt.PyJWKClient(cfg.jwks_url)
            _jwk_clients[cfg.jwks_url] = client
        return client.get_signing_key_from_jwt(token).key
    if _asymmetric(cfg.algorithms):
        if not cfg.public_key:
            raise jwt.InvalidTokenError("асимметричный алгоритм, но публичный ключ не задан")
        return cfg.public_key
    if not cfg.secret:
        raise jwt.InvalidTokenError("IDENTITY_JWT_SECRET/JWT_SECRET_KEY не задан")
    return cfg.secret


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
            claims = jwt.decode(
                token,
                _signing_key(token, cfg),
                algorithms=cfg.algorithms,
                audience=cfg.audience,
                issuer=cfg.issuer,
                options={"verify_aud": cfg.audience is not None},
            )
        else:
            claims = jwt.decode(token, options={"verify_signature": False})
    except (jwt.InvalidTokenError, jwt.PyJWKClientError):
        return None
    value = claims.get(cfg.claim)
    return str(value) if value else None
