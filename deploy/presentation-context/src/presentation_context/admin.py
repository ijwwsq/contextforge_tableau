"""Small protected editor for the single presentation style guide."""
from __future__ import annotations

import base64
import html
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route


def _path() -> Path:
    return Path(os.environ.get("GUIDELINES_PATH", "/app/catalog/presentation-guidelines.yml"))


# ── версионирование гайда (один документ): снимок всего файла на каждый save ──
HISTORY_KEEP = int(os.environ.get("GUIDE_HISTORY_KEEP", "50"))
_VERSION_FMT = "%Y%m%dT%H%M%S%fZ"
_VERSION_RE = re.compile(r"^\d{8}T\d{12}Z$")  # с микросекундами; защита от path traversal


def _history_dir() -> Path:
    d = _path().parent / "history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _snapshot_current() -> None:
    src = _path()
    if not src.exists():
        return
    ts = datetime.now(timezone.utc).strftime(_VERSION_FMT)
    dst = _history_dir() / f"guide.{ts}.yml"
    if not dst.exists():
        shutil.copy2(src, dst)
    _prune_history()


def _prune_history() -> None:
    if HISTORY_KEEP <= 0:
        return
    for old in sorted(_history_dir().glob("guide.*.yml"))[:-HISTORY_KEEP]:
        old.unlink(missing_ok=True)


def _list_versions() -> list[str]:
    ids = [p.name[len("guide."):-len(".yml")] for p in _history_dir().glob("guide.*.yml")]
    return sorted((v for v in ids if _VERSION_RE.match(v)), reverse=True)


def _version_path(version: str) -> Path | None:
    if not _VERSION_RE.match(version):
        return None
    p = _history_dir() / f"guide.{version}.yml"
    return p if p.exists() else None


def _fmt_version(v: str) -> str:
    try:
        return datetime.strptime(v, _VERSION_FMT).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return v


def _load() -> dict[str, Any]:
    data = yaml.safe_load(_path().read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("The style guide must be a YAML mapping.")
    return data


def _save_yaml(text: str) -> None:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("The style guide must be a YAML mapping.")
    _snapshot_current()  # версия ПЕРЕД перезаписью — история изменений
    target = _path()
    tmp = target.with_suffix(".yml.tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(target)


def _yaml_text() -> str:
    return yaml.safe_dump(_load(), allow_unicode=True, sort_keys=False)


async def _index(_request: Request) -> HTMLResponse:
    guide = html.escape(_yaml_text())
    return HTMLResponse(f"""<!doctype html><html lang=\"ru\"><meta charset=\"utf-8\"><title>Presentation guide</title>
<style>
body{{margin:0;background:#f5f7f5;color:#17211b;font:15px Arial,sans-serif}}main{{max-width:1000px;margin:0 auto;padding:32px 20px}}h1{{margin:0 0 6px;color:#005f3b}}p{{margin:0 0 20px;color:#52605a}}textarea{{width:100%;min-height:70vh;box-sizing:border-box;padding:16px;border:1px solid #b7c9bd;border-radius:6px;background:#fff;font:13px ui-monospace,monospace;line-height:1.45}}button{{margin-top:14px;padding:10px 18px;border:0;border-radius:4px;background:#006b3f;color:#fff;font-weight:700;cursor:pointer}}button:hover{{background:#004c2d}}code{{color:#806414}}
</style><main><h1>Presentation style guide</h1><p>YAML is the source used by <code>presentation-context</code>. &nbsp; <a href="/history">История версий →</a></p>
<form method=\"post\"><textarea name=\"guide\">{guide}</textarea><br><button type=\"submit\">Save</button></form></main></html>""")


async def _update(request: Request) -> HTMLResponse | RedirectResponse:
    try:
        _save_yaml(str((await request.form()).get("guide", "")))
    except (ValueError, yaml.YAMLError) as exc:
        return HTMLResponse(f"<p>{html.escape(str(exc))}</p><p><a href='/'>Back</a></p>", status_code=400)
    return RedirectResponse("/", status_code=303)


async def _history_get(_request: Request) -> HTMLResponse:
    versions = _list_versions()
    if not versions:
        rows = "<tr><td colspan='2' style='color:#8a978f'>История пуста — наполняется при сохранении.</td></tr>"
    else:
        rows = "".join(
            f"<tr><td>{html.escape(_fmt_version(v))}</td>"
            f"<td><a href='/history/{html.escape(v)}'>Смотреть</a> &nbsp; "
            f"<form method='post' action='/history/{html.escape(v)}/restore' style='display:inline'>"
            "<button onclick=\"return confirm('Восстановить эту версию? Текущая уйдёт в историю.')\">Restore</button>"
            "</form></td></tr>"
            for v in versions
        )
    return HTMLResponse(f"""<!doctype html><html lang="ru"><meta charset="utf-8"><title>История гайда</title>
<style>body{{margin:0;background:#f5f7f5;color:#17211b;font:15px Arial,sans-serif}}main{{max-width:1000px;margin:0 auto;padding:32px 20px}}
h1{{margin:0 0 16px;color:#005f3b}}table{{width:100%;border-collapse:collapse;background:#fff;border-radius:6px;overflow:hidden}}
th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid #e3ebe5}}th{{background:#eef4f0;color:#005f3b}}
a{{color:#006b3f}}button{{border:0;background:#006b3f;color:#fff;border-radius:4px;padding:3px 10px;cursor:pointer}}</style>
<main><h1>История версий гайда</h1><p><a href="/">← Назад к редактору</a></p>
<table><tr><th>Когда (UTC)</th><th></th></tr>{rows}</table></main></html>""")


async def _history_download(request: Request) -> Response:
    p = _version_path(request.path_params["version"])
    if p is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return PlainTextResponse(p.read_text(encoding="utf-8"))


async def _history_restore(request: Request) -> RedirectResponse:
    p = _version_path(request.path_params["version"])
    if p is None:
        return RedirectResponse("/history", status_code=303)
    _snapshot_current()  # текущее — в историю, restore обратим
    shutil.copy2(p, _path())
    return RedirectResponse("/", status_code=303)


async def _api_get(_request: Request) -> JSONResponse:
    return JSONResponse(_load())


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "guidelines_path": str(_path())})


class _BasicAuth:
    def __init__(self, app: Any, user: str, password: str):
        self.app, self._value = app, base64.b64encode(f"{user}:{password}".encode()).decode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            path = scope["path"]
            if path not in {"/healthz", "/api/guidelines"}:
                headers = dict(scope["headers"])
                if headers.get(b"authorization", b"").decode() != f"Basic {self._value}":
                    await JSONResponse({"error": "authentication_required"}, 401, {"WWW-Authenticate": 'Basic realm="presentation-context"'})(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def build_app() -> Any:
    password = os.environ.get("PRESENTATION_ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("PRESENTATION_ADMIN_PASSWORD must be set.")
    app = Starlette(routes=[
        Route("/", _index, methods=["GET"]),
        Route("/", _update, methods=["POST"]),
        Route("/history", _history_get, methods=["GET"]),
        Route("/history/{version}", _history_download, methods=["GET"]),
        Route("/history/{version}/restore", _history_restore, methods=["POST"]),
        Route("/api/guidelines", _api_get, methods=["GET"]),
        Route("/healthz", _health, methods=["GET"]),
    ])
    return _BasicAuth(app, os.environ.get("PRESENTATION_ADMIN_USER", "admin"), password)


def main() -> None:
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("HTTP_PORT", "8011")))


if __name__ == "__main__":
    main()
