"""Обмен персонального PAT на session-token Tableau + кэш сессий.

Сверено с REST API Tableau (help.tableau.com, rest_api_concepts_auth):
- Sign In:  POST /api/{version}/auth/signin
  body {credentials:{personalAccessTokenName, personalAccessTokenSecret,
       site:{contentUrl}}} → {credentials:{token, site:{id}, user:{id}}}
- Дальше token идёт в заголовке `X-Tableau-Auth`.
- Sign Out: POST /api/{version}/auth/signout (инвалидирует token).
- Session-token по умолчанию живёт 240 минут простоя.

КЛЮЧЕВОЕ ограничение PAT (security_personal_access_tokens): «Signing in again
with the same PAT ... will terminate the previous session and result in an
authentication error». Один PAT = одна активная сессия. Поэтому:
  1) сессия кэшируется на юзера и переиспользуется между запросами;
  2) вокруг signin — пер-юзерный лок, иначе два параллельных запроса одного
     юзера сделают два signin и убьют сессии друг другу;
  3) signout по завершении запроса НЕ делаем (сессия долгоживущая).
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

import httpx


def _verify_from_env() -> bool | str:
    """Значение httpx `verify` из TABLEAU_SSL_VERIFY.

    On-prem Tableau Server часто с внутренним/self-signed сертификатом:
      - `true` (дефолт) — строгая проверка (Cloud, публичный CA);
      - `false`         — не проверять (только доверенная сеть, не для прода);
      - путь к файлу    — свой CA-bundle (корректный вариант для on-prem).
    """
    raw = os.environ.get("TABLEAU_SSL_VERIFY", "true").strip()
    low = raw.lower()
    if low in ("true", "1", "yes", ""):
        return True
    if low in ("false", "0", "no"):
        return False
    return raw  # трактуем как путь к CA-bundle


@dataclass(frozen=True)
class TableauEndpoint:
    server: str
    site_content_url: str
    api_version: str = "3.22"
    verify: bool | str = True

    @classmethod
    def from_env(cls) -> "TableauEndpoint":
        return cls(
            server=os.environ["TABLEAU_SERVER"].rstrip("/"),
            site_content_url=os.environ.get("TABLEAU_SITE_NAME", ""),
            api_version=os.environ.get("TABLEAU_API_VERSION", "3.22"),
            verify=_verify_from_env(),
        )

    @property
    def base_url(self) -> str:
        return f"{self.server}/api/{self.api_version}"


@dataclass
class _Session:
    token: str
    site_id: str
    user_id: str
    expires_at: float


class TableauSignInError(RuntimeError):
    """Не удалось залогиниться PAT'ом (протух/отозван/неверный)."""


class SessionCache:
    """Кэш session-token'ов по ключу пользователя с пер-юзерным локом.

    TTL — проактивное обновление до истечения idle-таймаута Tableau; реальный
    сигнал протухания — 401 от upstream, по которому брокер зовёт invalidate().
    """

    def __init__(
        self,
        endpoint: TableauEndpoint,
        *,
        client: httpx.AsyncClient | None = None,
        ttl_seconds: float | None = None,
        timeout: float = 15.0,
    ):
        self._ep = endpoint
        self._client = client or httpx.AsyncClient(
            base_url=endpoint.base_url,
            timeout=timeout,
            verify=endpoint.verify,  # on-prem Server: свой CA / отключение проверки
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        # 230 мин < 240-мин дефолт Tableau — обновляемся заранее.
        self._ttl = ttl_seconds if ttl_seconds is not None else float(
            os.environ.get("TABLEAU_SESSION_TTL_SECONDS", 13800)
        )
        self._sessions: dict[str, _Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, user: str) -> asyncio.Lock:
        # Реестр локов защищаем своим локом — иначе гонка на создании lock'а.
        async with self._locks_guard:
            return self._locks.setdefault(user, asyncio.Lock())

    async def get_token(self, user: str, pat_name: str, pat_secret: str) -> str:
        cached = self._sessions.get(user)
        if cached and cached.expires_at > time.monotonic():
            return cached.token
        lock = await self._lock_for(user)
        async with lock:
            # Двойная проверка: пока ждали лок, сосед мог уже залогиниться.
            cached = self._sessions.get(user)
            if cached and cached.expires_at > time.monotonic():
                return cached.token
            session = await self._signin(pat_name, pat_secret)
            self._sessions[user] = session
            return session.token

    def invalidate(self, user: str) -> None:
        self._sessions.pop(user, None)

    async def _signin(self, pat_name: str, pat_secret: str) -> _Session:
        payload = {
            "credentials": {
                "personalAccessTokenName": pat_name,
                "personalAccessTokenSecret": pat_secret,
                "site": {"contentUrl": self._ep.site_content_url},
            }
        }
        try:
            r = await self._client.post("/auth/signin", json=payload)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TableauSignInError(
                f"Tableau signin для PAT '{pat_name}' отклонён: "
                f"{exc.response.status_code} {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise TableauSignInError(f"Tableau signin недоступен: {exc}") from exc
        creds = r.json()["credentials"]
        return _Session(
            token=creds["token"],
            site_id=creds["site"]["id"],
            user_id=creds["user"]["id"],
            expires_at=time.monotonic() + self._ttl,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
