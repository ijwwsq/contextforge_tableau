"""Единая админ-консоль с ролевым разделением (ТС 5.6, Уровень 2, без Keycloak).

Один вход → роль → доступ ТОЛЬКО к разрешённым разделам. Бэкенд-админки
(dashboard-context, tableau-identity, presentation-context) прячутся за консолью
(снимаются с хост-портов) и проксируются сюда с ролевым гейтингом. Аналитик
физически не попадает в раздел PAT-привязок (403, не просто скрытая ссылка).

Роли и разделы (кто что видит):
  analyst       → context, presentation          (Бизнес-аналитик)
  access-admin  → identity                        (Администратор доступа)
  admin         → всё                             (супер-админ)

Разделение реализовано без правки бэкендов: консоль реверс-проксирует их и
переписывает корневые ссылки/редиректы под префикс раздела.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

COOKIE = "console_session"
# hop-by-hop + то, что пересчитает httpx/Starlette
_STRIP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding",
          "upgrade", "cookie", "authorization"}


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    desc: str
    url: str
    auth: str  # "user:pass" для Basic-auth бэкенда


def _sections() -> dict[str, Section]:
    return {
        "context": Section(
            "context", "Бизнес-контекст дашбордов",
            "Описания, KPI, глоссарий, история изменений.",
            os.environ.get("CONTEXT_URL", "http://dashboard-context-admin:8010"),
            os.environ.get("CONTEXT_AUTH", "admin:changeme"),
        ),
        "presentation": Section(
            "presentation", "Правила презентаций",
            "Корпоративный стайл-гайд для ответов.",
            os.environ.get("PRESENTATION_URL", "http://presentation-context-admin:8011"),
            os.environ.get("PRESENTATION_AUTH", "admin:changeme"),
        ),
        "identity": Section(
            "identity", "Привязки Tableau (PAT)",
            "Сопоставление пользователь → учётка Tableau + персональный PAT.",
            os.environ.get("IDENTITY_URL", "http://tableau-identity-admin:8021"),
            os.environ.get("IDENTITY_AUTH", "admin:changeme"),
        ),
    }


ROLE_SECTIONS: dict[str, set[str]] = {
    "analyst": {"context", "presentation"},
    "access-admin": {"identity"},
    "admin": {"context", "presentation", "identity"},
}


def _secret() -> bytes:
    s = os.environ.get("CONSOLE_SESSION_SECRET") or os.environ.get("JWT_SECRET_KEY")
    if not s:
        raise RuntimeError("CONSOLE_SESSION_SECRET (или JWT_SECRET_KEY) не задан.")
    return s.encode()


def _role_password(role: str) -> str | None:
    env = {
        "analyst": "CONSOLE_ANALYST_PASSWORD",
        "access-admin": "CONSOLE_ACCESS_ADMIN_PASSWORD",
        "admin": "CONSOLE_ADMIN_PASSWORD",
    }.get(role)
    return os.environ.get(env) if env else None


# ── сессия: подписанная кука {role} ──────────────────────────────────────
def _sign(role: str) -> str:
    mac = hmac.new(_secret(), role.encode(), hashlib.sha256).hexdigest()
    return f"{role}.{mac}"


def _role_from_cookie(request: Request) -> str | None:
    raw = request.cookies.get(COOKIE, "")
    role, _, mac = raw.partition(".")
    if not role or not mac or role not in ROLE_SECTIONS:
        return None
    if hmac.compare_digest(mac, hmac.new(_secret(), role.encode(), hashlib.sha256).hexdigest()):
        return role
    return None


# ── страницы ──────────────────────────────────────────────────────────────
_STYLE = """<style>
:root{--bg:#0f1620;--surface:#16202c;--surface2:#1c2836;--border:#283747;--ink:#e7eef5;--muted:#93a1b1;--accent:#5aa9e6}
@media(prefers-color-scheme:light){:root{--bg:#f4f6f8;--surface:#fff;--surface2:#eef2f6;--border:#d7dee6;--ink:#16202e;--muted:#586472;--accent:#1f5c8f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:40px 20px}
h1{font-size:1.5rem;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 28px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
.card{display:block;text-decoration:none;color:inherit;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;transition:border-color .12s}
.card:hover{border-color:var(--accent)}.card h3{margin:0 0 6px;color:var(--accent)}.card p{margin:0;color:var(--muted);font-size:.9rem}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}
.pill{font:600 .74rem/1 ui-monospace,monospace;color:var(--accent);background:var(--surface2);border:1px solid var(--border);border-radius:999px;padding:6px 12px}
form.login{max-width:360px;margin:8vh auto;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:28px}
form.login input{width:100%;padding:11px;margin-top:10px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
form.login button{width:100%;margin-top:16px;padding:11px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-weight:700;cursor:pointer}
.err{color:#e06c6c;margin-top:12px;font-size:.88rem}a.logout{color:var(--muted);font-size:.85rem;text-decoration:none}
</style>"""


def _login_page(error: str = "") -> HTMLResponse:
    err = f'<div class="err">{error}</div>' if error else ""
    return HTMLResponse(f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<title>Admin console — вход</title>{_STYLE}<body>
<form class="login" method="post" action="/login">
  <h1>Admin console</h1><p class="sub">Вход по роли</p>
  <input name="role" placeholder="Роль (analyst / access-admin / admin)" autofocus>
  <input name="password" type="password" placeholder="Пароль">
  <button type="submit">Войти</button>{err}
</form></body></html>""")


def _home_page(role: str) -> HTMLResponse:
    allowed = ROLE_SECTIONS.get(role, set())
    secs = _sections()
    cards = "".join(
        f'<a class="card" href="/{s.key}/"><h3>{s.title}</h3><p>{s.desc}</p></a>'
        for k, s in secs.items() if k in allowed
    ) or '<p class="sub">Для этой роли нет доступных разделов.</p>'
    return HTMLResponse(f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<title>Admin console</title>{_STYLE}<body><div class="wrap">
  <div class="top"><span class="pill">роль: {role}</span><a class="logout" href="/logout">Выйти</a></div>
  <h1>Панель администрирования</h1>
  <p class="sub">Доступны только разделы вашей роли — остальное скрыто и закрыто на уровне доступа.</p>
  <div class="cards">{cards}</div>
</div></body></html>""")


# ── роуты ──────────────────────────────────────────────────────────────────
async def _login_get(request: Request) -> Response:
    if _role_from_cookie(request):
        return RedirectResponse("/", status_code=303)
    return _login_page()


async def _login_post(request: Request) -> Response:
    form = await request.form()
    role = str(form.get("role", "")).strip()
    password = str(form.get("password", ""))
    expected = _role_password(role)
    if not expected or not secrets.compare_digest(password, expected):
        return _login_page("Неверная роль или пароль.")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, _sign(role), httponly=True, samesite="lax", path="/")
    return resp


async def _logout(_request: Request) -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


async def _home(request: Request) -> Response:
    role = _role_from_cookie(request)
    if not role:
        return RedirectResponse("/login", status_code=303)
    return _home_page(role)


def _rewrite_html(body: bytes, prefix: str) -> bytes:
    # Корневые ссылки/действия бэкенда → под префикс раздела (без правки бэкенда).
    html = body.decode("utf-8", "replace")
    html = re.sub(r'(href|action|formaction)=(["\'])/(?!/)', rf'\1=\2{prefix}/', html)
    return html.encode("utf-8")


async def _proxy(request: Request) -> Response:
    role = _role_from_cookie(request)
    if not role:
        return RedirectResponse("/login", status_code=303)
    section = request.path_params["section"]
    secs = _sections()
    if section not in secs:
        return PlainTextResponse("Not found", status_code=404)
    if section not in ROLE_SECTIONS.get(role, set()):
        # Жёсткое разделение: роль без доступа не пускается в раздел.
        return PlainTextResponse("Доступ запрещён для вашей роли.", status_code=403)

    sec = secs[section]
    subpath = request.path_params.get("path", "")
    url = f"{sec.url.rstrip('/')}/{subpath}"
    prefix = f"/{section}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP}
    headers["Authorization"] = "Basic " + base64.b64encode(sec.auth.encode()).decode()
    body = await request.body()

    async with httpx.AsyncClient(timeout=30.0) as client:
        upstream = await client.request(
            request.method, url, headers=headers, content=body,
            params=request.query_params, follow_redirects=False,
        )

    # Редиректы бэкенда (Location: /...) → под префикс раздела.
    out_headers = {}
    for k, v in upstream.headers.items():
        lk = k.lower()
        if lk in {"content-length", "transfer-encoding", "content-encoding", "connection"}:
            continue
        if lk == "location" and v.startswith("/") and not v.startswith("//"):
            v = prefix + v
        out_headers[k] = v

    content = upstream.content
    ctype = upstream.headers.get("content-type", "")
    if "text/html" in ctype:
        content = _rewrite_html(content, prefix)

    return Response(content, status_code=upstream.status_code, headers=out_headers,
                    media_type=ctype or None)


async def _healthz(_request: Request) -> Response:
    return PlainTextResponse('{"status":"ok"}', media_type="application/json")


def build_app() -> Starlette:
    # Проверим, что хоть один ролевой пароль задан — иначе входить нечем.
    if not any(_role_password(r) for r in ROLE_SECTIONS):
        raise RuntimeError("Не задан ни один CONSOLE_*_PASSWORD.")
    _secret()  # ранняя валидация секрета
    return Starlette(routes=[
        Route("/healthz", _healthz, methods=["GET"]),
        Route("/login", _login_get, methods=["GET"]),
        Route("/login", _login_post, methods=["POST"]),
        Route("/logout", _logout, methods=["GET"]),
        Route("/", _home, methods=["GET"]),
        Route("/{section}", _proxy, methods=["GET", "POST", "PUT", "DELETE"]),
        Route("/{section}/{path:path}", _proxy, methods=["GET", "POST", "PUT", "DELETE"]),
    ])


def main() -> None:
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("HTTP_PORT", "8030")))


if __name__ == "__main__":
    main()
