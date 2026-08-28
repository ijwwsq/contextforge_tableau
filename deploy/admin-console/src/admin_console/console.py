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
# Единый светлый Notion-like визуал: тёплый near-black текст, тонкие серые
# границы, один сдержанный синий акцент, без градиентов/теней/блобов.
_STYLE = """<style>
:root{--bg:#ffffff;--surface:#ffffff;--surface2:#f7f7f5;--border:#e9e9e7;
--text:#37352f;--muted:#787774;--accent:#2383e2;--hover:#f1f1ef;--danger:#c0271f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:56px 24px}
h1{font-size:1.7rem;font-weight:700;letter-spacing:-.01em;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 32px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.card{display:block;text-decoration:none;color:inherit;background:var(--surface);
border:1px solid var(--border);border-radius:8px;padding:18px 20px;transition:background .1s,border-color .1s}
.card:hover{background:var(--hover)}
.card h3{margin:0 0 4px;font-size:1rem;font-weight:600}
.card p{margin:0;color:var(--muted);font-size:.88rem}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:32px}
.pill{font:500 .8rem/1 ui-sans-serif,sans-serif;color:var(--muted);background:var(--surface2);
border:1px solid var(--border);border-radius:6px;padding:6px 12px}
form.login{max-width:340px;margin:14vh auto;background:var(--surface);
border:1px solid var(--border);border-radius:10px;padding:28px 26px}
form.login h1{font-size:1.35rem}
form.login input{width:100%;padding:10px 12px;margin-top:10px;border:1px solid var(--border);
border-radius:6px;background:var(--surface);color:var(--text);font:inherit}
form.login input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 2px #2383e233}
form.login button{width:100%;margin-top:18px;padding:10px;border:0;border-radius:6px;
background:var(--accent);color:#fff;font-weight:600;font-size:.95rem;cursor:pointer}
form.login button:hover{background:#1a6fc4}
.err{color:var(--danger);margin-top:12px;font-size:.88rem}
a.logout{color:var(--muted);font-size:.88rem;text-decoration:none}a.logout:hover{color:var(--text)}
</style>"""

# Инжектируется в проксируемые страницы бэкендов: переопределяет их
# CSS-переменные под Notion-палитру, форсит светлую тему и гасит «иишный» декор
# (градиенты, блобы, тяжёлые тени, градиентный текст), не трогая исходники.
_INJECT_THEME = """<style id="unified-theme">
:root{
  --bg:#ffffff;--surface:#ffffff;--surface-2:#f7f7f5;--surface2:#f7f7f5;
  --border:#e9e9e7;--row-hover:#f7f7f5;--pill-bg:#f1f1ef;
  --text:#37352f;--ink:#37352f;--text-muted:#787774;--muted:#787774;
  --primary:#2383e2;--primary-2:#2383e2;--primary-hover:#1a6fc4;--primary-text:#ffffff;
  --accent:#2383e2;--accent-basic:#2383e2;--accent-content:#2383e2;--accent-metrics:#2383e2;
  --danger:#c0271f;--danger-bg:#fdeeed;--shadow:none;--radius:8px;
  --blob-a:transparent;--blob-b:transparent;--blob-c:transparent;
}
html,body{background:#ffffff!important;color:#37352f!important;
font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif!important}
body::before,body::after{display:none!important}                 /* декоративные блобы */
*{box-shadow:none!important;backdrop-filter:none!important}
h1,.topbar h1,.brand-mark{background:none!important;-webkit-text-fill-color:#37352f!important;color:#37352f!important}
.brand-mark{background:#2383e2!important;-webkit-text-fill-color:#fff!important;color:#fff!important}
button.primary,.btn.primary,form.login button,.addform .save,button:hover{filter:none!important}
button.primary,.btn.primary,.addform .save,.save{background:#2383e2!important;border-color:#2383e2!important;color:#fff!important}
.topbar,.card,.section,.list th{background:#ffffff!important}
.section{border-left-color:#e9e9e7!important}
.pill{background:#f1f1ef!important;color:#787774!important}
a{color:#2383e2!important}
/* Красивый таймлайн истории записи (Notion-стиль поверх единой темы) */
.timeline::before{background:#e9e9e7!important}
.tl-dot{background:#2383e2!important;box-shadow:0 0 0 3px #ffffff!important;border-radius:50%!important}
.tl-node{background:#f7f7f5!important;border:1px solid #e9e9e7!important;border-radius:8px!important;padding:10px 14px!important}
.tl-time{color:#787774!important;font-family:ui-monospace,monospace!important}
.tl-diff{line-height:2!important}
.tl-diff .pill{background:#ececeb!important;color:#37352f!important;border:1px solid #e0e0de!important;border-radius:4px!important;padding:2px 7px!important}
.tl-diff b{font-weight:600!important;color:#37352f!important}
.section h2{color:#787774!important}
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
    # Единый визуал: инжектируем тему последней в <head>, чтобы перебить стили бэкенда.
    if "</head>" in html:
        html = html.replace("</head>", _INJECT_THEME + "</head>", 1)
    else:
        html = _INJECT_THEME + html
    return html.encode("utf-8")


def _section_down_page(title: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="ru"><meta charset="utf-8"><title>{title}</title>{_STYLE}
<body><div class="wrap"><div class="top"><span class="pill">раздел недоступен</span>
<a class="logout" href="/">← На главную</a></div>
<h1>{title}</h1><p class="sub">Сервис этого раздела сейчас недоступен. Попробуйте позже
или проверьте, что соответствующий контейнер запущен.</p></div></body></html>""",
        status_code=502,
    )


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

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream = await client.request(
                request.method, url, headers=headers, content=body,
                params=request.query_params, follow_redirects=False,
            )
    except httpx.RequestError:
        # Бэкенд раздела не поднят/недоступен — не роняем консоль.
        return _section_down_page(sec.title)

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
