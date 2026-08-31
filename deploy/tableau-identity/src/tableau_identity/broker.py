"""Брокер per-user аутентификации перед tableau-mcp.

Стоит в пути федерации: гейт регистрирует ЕГО как MCP-сервер `tableau`, а он
прозрачно проксирует MCP-трафик в реальный tableau-mcp, подставляя на каждый
запрос `x-tableau-auth` — session-token, полученный обменом персонального PAT
привязанного пользователя (см. tableau_auth). Так вызовы инструментов идут в
Tableau от имени личной учётки, наследуя её права (RLS) — требование ТС 5.4.2.

Хендшейк MCP (initialize/tools/list/ping) в Tableau не ходит и токена не
требует — пропускаем как есть, чтобы работала регистрация федерации гейтом.
`tools/call` без привязки — чистая JSON-RPC-ошибка (fallback на общий PAT
исключён на уровне брокера).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .identity import IdentityConfig, extract_user
from .store import MappingStore, default_store
from .tableau_auth import SessionCache, TableauEndpoint, TableauSignInError

log = logging.getLogger("tableau_identity.broker")

# Отдельный логгер аудита: по одной JSON-строке на tool-вызов (учётка ↔ инструмент).
# Ядро гейта не трогаем — брокер видит и личность, и имя инструмента сразу.
# Формат намеренно простой (structured log в stdout); переезд в БД — когда
# определитесь с форматом хранения.
audit_log = logging.getLogger("tableau_identity.audit")


def _emit_audit(**fields: Any) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    record.update({k: v for k, v in fields.items() if v is not None})
    audit_log.info(json.dumps(record, ensure_ascii=False))

# Заголовки, которые нельзя переносить между hop'ами как есть.
_HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "proxy-authenticate", "proxy-authorization", "te", "trailer",
}
# Плюс consumed identity — дальше её не пробрасываем.
_STRIP_REQUEST = _HOP_BY_HOP | {"authorization", "x-upstream-authorization", "x-tableau-auth"}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _parse_rpc(body: bytes) -> dict[str, Any] | None:
    """Разобрать одиночный JSON-RPC вызов; батчи/мусор → None (просто проксируем)."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


class Broker:
    def __init__(self, store: MappingStore, sessions: SessionCache, upstream: str, client: httpx.AsyncClient):
        self._store = store
        self._sessions = sessions
        self._upstream = upstream
        self._client = client
        self._id = IdentityConfig.from_env()

    async def handle(self, request: Request) -> Response:
        body = await request.body()
        user = extract_user({k.lower(): v for k, v in request.headers.items()}, self._id)

        token: str | None = None
        if request.method == "POST":
            rpc = _parse_rpc(body)
            if rpc is not None and rpc.get("method") == "tools/call":
                # Только реальный вызов инструмента требует личность + привязку.
                tool = str((rpc.get("params") or {}).get("name") or "")
                if not user:
                    _emit_audit(event="blocked", reason="no_identity", tool=tool)
                    return _jsonrpc_error(
                        rpc.get("id"), -32001,
                        "Требуется аутентификация: не найден токен пользователя.",
                    )
                mapping = self._store.get(user)
                if mapping is None:
                    _emit_audit(event="blocked", reason="no_mapping", user=user, tool=tool)
                    return _jsonrpc_error(
                        rpc.get("id"), -32001,
                        f"Для пользователя '{user}' не привязана учётная запись Tableau. "
                        "Обратитесь к администратору доступа.",
                    )
                try:
                    token = await self._sessions.get_token(user, mapping.pat_name, mapping.pat_secret)
                except TableauSignInError as exc:
                    _emit_audit(event="error", reason="signin", user=user, tool=tool,
                                tableau_user=mapping.tableau_username)
                    return _jsonrpc_error(rpc.get("id"), -32002, str(exc))
                resp = await self._proxy(request, body, user, token)
                # Главная запись аудита: учётка X вызвала инструмент Y (под учёткой Tableau Z).
                _emit_audit(event="call", user=user, tool=tool,
                            tableau_user=mapping.tableau_username, status=resp.status_code)
                return resp

        return await self._proxy(request, body, user, token)

    async def _proxy(self, request: Request, body: bytes, user: str | None, token: str | None) -> Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQUEST}
        if token:
            headers["X-Tableau-Auth"] = token

        for attempt in (1, 2):
            upstream_req = self._client.build_request(
                request.method, self._upstream, headers=headers, content=body,
                params=request.query_params,
            )
            resp = await self._client.send(upstream_req, stream=True)
            # Протухшую сессию видно как 401 от passthrough-мидлвари tableau-mcp:
            # инвалидируем и логинимся заново ровно один раз.
            if resp.status_code == 401 and token and user and attempt == 1:
                await resp.aclose()
                self._sessions.invalidate(user)
                mapping = self._store.get(user)
                if mapping is None:
                    break
                try:
                    token = await self._sessions.get_token(user, mapping.pat_name, mapping.pat_secret)
                except TableauSignInError as exc:
                    return _jsonrpc_error(None, -32002, str(exc))
                headers["X-Tableau-Auth"] = token
                continue
            return self._stream_back(resp)
        return self._stream_back(resp)

    def _stream_back(self, resp: httpx.Response) -> StreamingResponse:
        # content-type несёт media_type; content-length/encoding пересчитает стрим.
        out_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in _HOP_BY_HOP | {"content-encoding", "content-type"}
        }

        async def body_iter() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(
            body_iter(),
            status_code=resp.status_code,
            headers=out_headers,
            media_type=resp.headers.get("content-type"),
        )


def _broker(app: Starlette) -> Broker:
    return app.state.broker


async def _handle(request: Request) -> Response:
    return await _broker(request.app).handle(request)


async def _health(request: Request) -> JSONResponse:
    try:
        count = len(_broker(request.app)._store.list())
        ok = True
    except Exception as exc:  # каталог/ключ сломаны — не «ok»
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)
    return JSONResponse({"status": "ok", "mappings": count, "upstream_ok": ok})


@contextlib.asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    store = default_store()
    sessions = SessionCache(TableauEndpoint.from_env())
    upstream = os.environ["TABLEAU_MCP_URL"]
    client = httpx.AsyncClient(timeout=float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", 60)))
    app.state.broker = Broker(store, sessions, upstream, client)
    try:
        yield
    finally:
        await sessions.aclose()
        await client.aclose()


def build_app() -> Starlette:
    base = os.environ.get("BROKER_BASE_PATH", "/tableau-mcp")
    return Starlette(
        routes=[
            Route("/healthz", _health, methods=["GET"]),
            Route(base, _handle, methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=_lifespan,
    )


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(
        build_app(), host="0.0.0.0", port=int(os.environ.get("HTTP_PORT", "8020")),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
