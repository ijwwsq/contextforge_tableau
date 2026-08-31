"""Веб-админка 1:1-маппинга «пользователь ↔ учётная запись Tableau (PAT)».

Требование ТС 5.6.2: отдельный интерфейс администратора доступа для управления
сопоставлением. HTML-редактор и REST закрыты Basic-auth; PAT-секрет вводится
здесь и уходит в хранилище зашифрованным (store), наружу не отдаётся никогда.
"""
from __future__ import annotations

import base64
import html
import logging
import os
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from .store import MappingError, MappingStore, default_store


def _store(request: Request) -> MappingStore:
    return request.app.state.store


def _row(m: Any) -> str:
    u, t, p = html.escape(m.user), html.escape(m.tableau_username), html.escape(m.pat_name)
    return (
        f"<tr><td>{u}</td><td>{t}</td><td>{p}</td><td>••••••</td>"
        f"<td><form method='post' action='/delete' onsubmit=\"return confirm('Удалить {u}?')\">"
        f"<input type='hidden' name='user' value='{u}'><button class='del'>×</button></form></td></tr>"
    )


async def _index(request: Request) -> HTMLResponse:
    rows = "".join(_row(m) for m in _store(request).list()) or (
        "<tr><td colspan='5' class='empty'>Привязок пока нет.</td></tr>"
    )
    return HTMLResponse(f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<title>Tableau identity</title><style>
body{{margin:0;background:#f5f7f5;color:#17211b;font:15px Arial,sans-serif}}
main{{max-width:900px;margin:0 auto;padding:32px 20px}}h1{{margin:0 0 6px;color:#005f3b}}
p{{margin:0 0 20px;color:#52605a}}table{{width:100%;border-collapse:collapse;background:#fff;border-radius:6px;overflow:hidden}}
th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid #e3ebe5}}th{{background:#eef4f0;color:#005f3b}}
.empty{{color:#8a978f;text-align:center}}.del{{border:0;background:#c0392b;color:#fff;border-radius:4px;padding:2px 9px;cursor:pointer;font-weight:700}}
form.add{{margin-top:24px;background:#fff;padding:18px;border-radius:6px;display:grid;gap:10px;grid-template-columns:1fr 1fr}}
form.add input{{padding:9px;border:1px solid #b7c9bd;border-radius:4px;font:14px Arial}}
form.add button{{grid-column:1/3;padding:10px;border:0;border-radius:4px;background:#006b3f;color:#fff;font-weight:700;cursor:pointer}}
form.add button:hover{{background:#004c2d}}h2{{color:#005f3b;margin:28px 0 0}}
</style><main><h1>Tableau identity — привязки 1:1</h1>
<p>Каждый ИИ-пользователь → личная учётка Tableau и её персональный PAT. Вызовы идут от имени этой учётки (RLS).</p>
<table><tr><th>Пользователь</th><th>Учётка Tableau</th><th>PAT name</th><th>PAT secret</th><th></th></tr>{rows}</table>
<h2>Добавить / обновить</h2>
<form class="add" method="post">
<input name="user" placeholder="Пользователь (sub из токена, обычно email)" required>
<input name="tableau_username" placeholder="Имя учётной записи Tableau" required>
<input name="pat_name" placeholder="PAT name" required>
<input name="pat_secret" placeholder="PAT secret" required>
<button type="submit">Сохранить</button></form></main></html>""")


async def _upsert(request: Request) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    try:
        _store(request).put(
            user=str(form.get("user", "")),
            tableau_username=str(form.get("tableau_username", "")),
            pat_name=str(form.get("pat_name", "")),
            pat_secret=str(form.get("pat_secret", "")),
        )
    except MappingError as exc:
        return HTMLResponse(
            f"<p>{html.escape(str(exc))}</p><p><a href='/'>Назад</a></p>", status_code=400
        )
    return RedirectResponse("/", status_code=303)


async def _delete(request: Request) -> RedirectResponse:
    _store(request).delete(str((await request.form()).get("user", "")))
    return RedirectResponse("/", status_code=303)


async def _api_list(request: Request) -> JSONResponse:
    # Только публичные поля — секрет наружу не отдаём.
    return JSONResponse({"mappings": [m.public() for m in _store(request).list()]})


async def _health(request: Request) -> JSONResponse:
    try:
        count = len(_store(request).list())
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)
    return JSONResponse({"status": "ok", "mappings": count})


class _BasicAuth:
    """Basic-auth на всё, кроме /healthz."""

    def __init__(self, app: Any, user: str, password: str):
        self.app = app
        self._value = base64.b64encode(f"{user}:{password}".encode()).decode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] != "/healthz":
            headers = dict(scope["headers"])
            if headers.get(b"authorization", b"").decode() != f"Basic {self._value}":
                await JSONResponse(
                    {"error": "authentication_required"}, 401,
                    {"WWW-Authenticate": 'Basic realm="tableau-identity"'},
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app() -> Any:
    password = os.environ.get("IDENTITY_ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("IDENTITY_ADMIN_PASSWORD must be set.")
    app = Starlette(routes=[
        Route("/", _index, methods=["GET"]),
        Route("/", _upsert, methods=["POST"]),
        Route("/delete", _delete, methods=["POST"]),
        Route("/api/mappings", _api_list, methods=["GET"]),
        Route("/healthz", _health, methods=["GET"]),
    ])
    store = default_store()
    try:  # разовая миграция YAML→Postgres (если включён PG и БД пустая)
        moved = store.migrate_from_file_if_empty()
        if moved:
            logging.getLogger("tableau_identity.admin").info(
                "Перенесено привязок из YAML в Postgres: %d.", moved)
    except Exception as exc:  # миграция best-effort — не роняем старт
        logging.getLogger("tableau_identity.admin").warning("Миграция маппингов YAML->PG пропущена: %s", exc)
    app.state.store = store
    return _BasicAuth(app, os.environ.get("IDENTITY_ADMIN_USER", "admin"), password)


def main() -> None:
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("HTTP_PORT", "8021")))


if __name__ == "__main__":
    main()
