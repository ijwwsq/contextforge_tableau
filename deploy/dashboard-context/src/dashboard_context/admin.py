from __future__ import annotations

import base64
import html
import logging
import os
from pathlib import Path
from typing import Any

import uvicorn
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

log = logging.getLogger("dashboard_context.admin")

FIELDS_TEXT = ("luid", "slug", "name", "owner", "purpose", "audience", "freshness_sla")
FIELDS_BLOCK = ("description", "how_built", "notes")

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 100000  # don't let long lines get hard-wrapped back into flow junk
_yaml.indent(mapping=2, sequence=4, offset=2)  # matches this file's existing "  - key:" list style


def _catalog_path() -> Path:
    return Path(os.environ.get("CATALOG_PATH", "/app/catalog/dashboards.yml"))


def _load() -> CommentedMap:
    path = _catalog_path()
    with path.open("r", encoding="utf-8", newline="") as f:
        data = _yaml.load(f)
    if data is None:
        data = CommentedMap()
    if "dashboards" not in data:
        data["dashboards"] = CommentedSeq()
    return data


def _save(data: CommentedMap) -> None:
    path = _catalog_path()
    tmp = path.with_suffix(".yml.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        _yaml.dump(data, f)
    tmp.replace(path)  # same filesystem -> atomic, Catalog's mtime check sees one clean change


def _entry_key(entry: dict) -> str:
    return str(entry.get("slug") or entry.get("luid") or "")


def _find(data: CommentedMap, key: str) -> CommentedMap | None:
    for entry in data.get("dashboards", []):
        if _entry_key(entry) == key:
            return entry
    return None


def _kpis_to_text(entry: dict) -> str:
    return "\n".join(str(k) for k in (entry.get("kpis") or []))


def _text_to_kpis(text: str) -> CommentedSeq:
    seq = CommentedSeq()
    for line in text.splitlines():
        line = line.strip().lstrip("-").strip()
        if line:
            seq.append(line)
    return seq


def _glossary_to_text(entry: dict) -> str:
    return "\n".join(f"{k}: {v}" for k, v in (entry.get("glossary") or {}).items())


def _text_to_glossary(text: str) -> CommentedMap:
    out = CommentedMap()
    for line in text.splitlines():
        if not line.strip() or ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------- rendering

_FIELD_LABELS = {
    "luid": "LUID",
    "slug": "Slug",
    "name": "Name",
    "owner": "Owner",
    "purpose": "Purpose",
    "audience": "Audience",
    "freshness_sla": "Freshness SLA",
    "description": "Description",
    "how_built": "How built",
    "notes": "Notes",
}

_FIELD_HINTS = {
    "luid": "Tableau view LUID — preferred anchor",
    "slug": "short stable alias, e.g. sales-weekly",
    "description": "what a user sees: sheets, views, filters",
    "how_built": "data sources, joins, calculated fields, caveats",
    "notes": "gotchas, known caveats",
}

# self-contained — no CDN fonts/scripts, this project cares about air-gapped deploys (see REVIEW.md)
_FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E"
    "%3Cstop offset='0' stop-color='%236690ff'/%3E%3Cstop offset='1' stop-color='%23b06bf2'/%3E"
    "%3C/linearGradient%3E%3C/defs%3E"
    "%3Crect width='64' height='64' rx='16' fill='url(%23g)'/%3E"
    "%3Ctext x='32' y='42' font-size='30' text-anchor='middle' fill='white' "
    "font-family='monospace' font-weight='bold'%3E%7B%7D%3C/text%3E%3C/svg%3E"
)

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<link rel="icon" href='""" + _FAVICON + """'>
<style>
  :root {{
    --bg: #f4f5f9; --surface: #ffffff; --surface-2: #fbfbfd; --border: #e4e6ed; --row-hover: #f2f4ff;
    --text: #14161c; --text-muted: #676e7d; --pill-bg: #eef0f6;
    --primary: #4b5fe4; --primary-2: #8a4fe0; --primary-hover: #3c4fd1; --primary-text: #ffffff;
    --danger: #c0271f; --danger-bg: #fdeeed;
    --blob-a: rgba(99, 102, 241, .16); --blob-b: rgba(217, 70, 239, .13); --blob-c: rgba(45, 212, 191, .12);
    --accent-basic: #4b5fe4; --accent-content: #a24fe0; --accent-metrics: #0ea5a0;
    --radius: 12px; --shadow: 0 1px 2px rgba(16,24,40,.05), 0 4px 16px rgba(16,24,40,.06);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0b0d12; --surface: #151821; --surface-2: #11141b; --border: #262b37; --row-hover: #1c2130;
      --text: #e9ebf1; --text-muted: #97a0b3; --pill-bg: #1f2430;
      --primary: #7c8bff; --primary-2: #c37bff; --primary-hover: #97a3ff; --primary-text: #0b0d12;
      --danger: #f3766e; --danger-bg: #2a1616;
      --blob-a: rgba(124, 139, 255, .20); --blob-b: rgba(195, 123, 255, .16); --blob-c: rgba(45, 212, 191, .14);
      --accent-basic: #8b96ff; --accent-content: #d19bff; --accent-metrics: #4fd9d2;
      --shadow: 0 1px 2px rgba(0,0,0,.5), 0 4px 20px rgba(0,0,0,.4);
    }}
  }}
  * {{ box-sizing: border-box; }}
  html {{ background: var(--bg); }}
  body {{
    margin: 0; min-height: 100vh; background: var(--bg); color: var(--text); position: relative;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  body::before {{
    content: ""; position: fixed; inset: 0; z-index: 0; pointer-events: none;
    background:
      radial-gradient(38rem 22rem at 8% -8%, var(--blob-a), transparent 60%),
      radial-gradient(30rem 20rem at 108% 12%, var(--blob-b), transparent 60%),
      radial-gradient(26rem 18rem at 50% 118%, var(--blob-c), transparent 60%);
  }}
  a {{ color: var(--primary); }}
  .topbar {{ position: sticky; top: 0; z-index: 10; background: color-mix(in srgb, var(--surface) 88%, transparent); backdrop-filter: blur(10px); border-bottom: 1px solid var(--border); }}
  .topbar-inner {{
    max-width: 1000px; margin: 0 auto; padding: .9rem 1.25rem;
    display: flex; justify-content: space-between; align-items: center;
  }}
  .brand {{ display: flex; align-items: center; gap: .65rem; }}
  .brand-mark {{
    width: 30px; height: 30px; border-radius: 8px; flex: none;
    background: linear-gradient(135deg, var(--primary), var(--primary-2));
    display: flex; align-items: center; justify-content: center;
    font-family: ui-monospace, monospace; font-weight: 700; font-size: .85rem; color: #fff;
    box-shadow: 0 2px 8px rgba(75, 95, 228, .35);
  }}
  .topbar h1 {{
    font-size: .95rem; font-weight: 700; margin: 0; letter-spacing: -.01em;
    background: linear-gradient(90deg, var(--text), var(--text) 60%, var(--primary));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }}
  .topbar .sub {{ color: var(--text-muted); font-size: .76rem; margin-top: .05rem; }}
  main.wrap {{ position: relative; z-index: 1; max-width: 1000px; margin: 0 auto; padding: 1.75rem 1.25rem 4rem; }}

  .card {{
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow);
    overflow: hidden; transition: box-shadow .15s ease, transform .15s ease;
  }}
  .searchbox {{ position: relative; margin-bottom: 1rem; }}
  .searchbox svg {{ position: absolute; left: .7rem; top: 50%; transform: translateY(-50%); opacity: .5; pointer-events: none; }}
  .searchbox input {{
    width: 100%; padding: .65rem .8rem .65rem 2.2rem; font: inherit; font-size: .9rem;
    border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); color: var(--text);
    transition: border-color .12s ease, box-shadow .12s ease;
  }}
  .searchbox input:focus {{ outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 18%, transparent); }}

  table.list {{ width: 100%; border-collapse: collapse; }}
  .list th {{
    text-align: left; font-size: .68rem; text-transform: uppercase; letter-spacing: .06em;
    color: var(--text-muted); padding: .7rem 1rem; border-bottom: 1px solid var(--border); background: var(--surface-2);
  }}
  .list td {{ padding: .65rem 1rem; border-bottom: 1px solid var(--border); font-size: .88rem; vertical-align: middle; }}
  .list tr:last-child td {{ border-bottom: none; }}
  .list tr {{ transition: background .1s ease; }}
  .list tr:hover td {{ background: var(--row-hover); }}
  .list a.name {{ color: var(--text); font-weight: 600; text-decoration: none; }}
  .list a.name:hover {{ color: var(--primary); }}
  .pill {{
    display: inline-block; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .74rem;
    color: var(--primary); background: color-mix(in srgb, var(--primary) 12%, var(--pill-bg));
    padding: .15rem .55rem; border-radius: 999px;
  }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .78rem; color: var(--text-muted); }}
  .muted {{ color: var(--text-muted); }}
  .empty {{ padding: 3.5rem 1rem; text-align: center; color: var(--text-muted); }}
  .empty svg {{ opacity: .55; margin-bottom: .5rem; }}
  .count {{ margin-top: .75rem; font-size: .8rem; color: var(--text-muted); }}

  .breadcrumb {{ margin-bottom: 1rem; font-size: .85rem; }}
  .section {{
    background: var(--surface); border: 1px solid var(--border); border-left: 3px solid var(--accent);
    border-radius: var(--radius); box-shadow: var(--shadow); padding: 1.25rem; margin-bottom: 1.1rem;
  }}
  .section--basic {{ --accent: var(--accent-basic); }}
  .section--content {{ --accent: var(--accent-content); }}
  .section--metrics {{ --accent: var(--accent-metrics); }}
  .section h2 {{
    font-size: .74rem; text-transform: uppercase; letter-spacing: .06em; color: var(--text-muted);
    margin: 0 0 1rem; font-weight: 700; display: flex; align-items: center; gap: .45rem;
  }}
  .section h2::before {{
    content: ""; width: 7px; height: 7px; border-radius: 999px; background: var(--accent); flex: none;
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 20%, transparent);
  }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }}
  @media (max-width: 640px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}
  .field {{ margin-bottom: 1rem; }}
  .field:last-child {{ margin-bottom: 0; }}
  .field label {{ display: block; font-size: .8rem; font-weight: 600; margin-bottom: .35rem; }}
  .field .hint {{ font-size: .74rem; color: var(--text-muted); margin-top: .3rem; }}
  input[type=text], textarea {{
    width: 100%; font: inherit; font-size: .88rem; padding: .55rem .7rem;
    border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); color: var(--text);
    transition: border-color .12s ease, box-shadow .12s ease;
  }}
  input[type=text]:focus, textarea:focus {{
    outline: none; border-color: var(--accent, var(--primary));
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent, var(--primary)) 18%, transparent);
  }}
  textarea {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; line-height: 1.5; resize: vertical; }}

  .actions {{ display: flex; align-items: center; gap: .6rem; margin-top: .25rem; }}
  button, .btn {{
    display: inline-flex; align-items: center; gap: .4rem; padding: .55rem 1.1rem; border-radius: 8px;
    border: 1px solid var(--border); background: var(--surface); color: var(--text); font: inherit;
    font-size: .85rem; font-weight: 600; cursor: pointer; text-decoration: none;
    transition: background .12s ease, border-color .12s ease, color .12s ease, transform .08s ease, box-shadow .12s ease;
  }}
  button:hover, .btn:hover {{ border-color: var(--primary); color: var(--primary); }}
  button:active, .btn:active {{ transform: translateY(1px); }}
  button.primary {{
    background: linear-gradient(135deg, var(--primary), var(--primary-2)); border-color: transparent; color: var(--primary-text);
    box-shadow: 0 2px 10px color-mix(in srgb, var(--primary) 35%, transparent);
  }}
  button.primary:hover {{ filter: brightness(1.06); color: var(--primary-text); border-color: transparent; }}
  button.danger {{ color: var(--danger); margin-left: auto; }}
  button.danger:hover {{ border-color: var(--danger); background: var(--danger-bg); }}
  .btn.ghost {{ border-color: transparent; background: transparent; padding-left: 0; }}
</style></head>
<body>
<header class="topbar"><div class="topbar-inner">
  <div class="brand">
    <div class="brand-mark">{{}}</div>
    <div><h1>dashboard-context</h1><div class="sub">Catalog admin</div></div>
  </div>
  <a class="btn primary" href="/new">+ New dashboard</a>
</div></header>
<main class="wrap">
{body}
</main>
</body></html>"""


def _render(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), body=body))


_SEARCH_ICON = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/>'
    '<line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>'
)
_EMPTY_ICON = (
    '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="4" width="18" height="14" rx="2"/><path d="M3 9h18"/><path d="M8 4v5"/></svg>'
)


def _list_body(data: CommentedMap) -> str:
    entries = list(data.get("dashboards", []))
    if not entries:
        return (
            '<div class="card"><div class="empty">'
            f"{_EMPTY_ICON}<div>No dashboards yet. <a href='/new'>Add the first one →</a></div>"
            "</div></div>"
        )

    rows = []
    for entry in entries:
        key = html.escape(_entry_key(entry))
        name = html.escape(str(entry.get("name") or ""))
        owner = html.escape(str(entry.get("owner") or "")) or '<span class="muted">—</span>'
        luid = html.escape(str(entry.get("luid") or "")) or '<span class="muted">—</span>'
        slug = html.escape(str(entry.get("slug") or "")) or '<span class="muted">—</span>'
        rows.append(
            f"<tr><td><a class='name' href='/edit/{key}'>{name or key}</a></td>"
            f"<td><span class='pill'>{slug}</span></td>"
            f"<td class='mono'>{luid}</td><td>{owner}</td></tr>"
        )
    count = len(entries)
    return f"""
    <div class="searchbox">{_SEARCH_ICON}<input type="text" id="q" placeholder="Search by name, slug or LUID…" oninput="filterRows()"></div>
    <div class="card">
      <table class="list">
        <thead><tr><th>Name</th><th>Slug</th><th>LUID</th><th>Owner</th></tr></thead>
        <tbody id="rows">{''.join(rows)}</tbody>
      </table>
    </div>
    <p class="count">{count} dashboard{'s' if count != 1 else ''} in catalog.</p>
    <script>
      function filterRows() {{
        const q = document.getElementById('q').value.toLowerCase();
        document.querySelectorAll('#rows tr').forEach(tr => {{
          tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none';
        }});
      }}
    </script>
    """


def _form_body(entry: dict, key: str, is_new: bool) -> str:
    def val(name: str) -> str:
        return html.escape(str(entry.get(name) or ""))

    def field(name: str, *, block: bool = False, rows: int = 4) -> str:
        label = _FIELD_LABELS.get(name, name)
        hint = f"<div class='hint'>{_FIELD_HINTS[name]}</div>" if name in _FIELD_HINTS else ""
        widget = (
            f"<textarea name='{name}' rows='{rows}'>{val(name)}</textarea>"
            if block
            else f"<input type='text' name='{name}' value='{val(name)}'>"
        )
        return f"<div class='field'><label>{label}</label>{widget}{hint}</div>"

    basic_info = f"""
    <div class="section section--basic">
      <h2>Basic info</h2>
      <div class="grid2">{field('luid')}{field('slug')}</div>
      <div class="grid2">{field('name')}{field('owner')}</div>
      <div class="grid2">{field('purpose')}{field('audience')}</div>
      {field('freshness_sla')}
    </div>
    """
    content = f"""
    <div class="section section--content">
      <h2>Content</h2>
      {field('description', block=True, rows=6)}
      {field('how_built', block=True, rows=8)}
      {field('notes', block=True, rows=4)}
    </div>
    """
    kpis_text = html.escape(_kpis_to_text(entry))
    glossary_text = html.escape(_glossary_to_text(entry))
    metrics = f"""
    <div class="section section--metrics">
      <h2>Metrics &amp; glossary</h2>
      <div class="grid2">
        <div class="field"><label>KPIs</label><textarea name='kpis' rows='5'>{kpis_text}</textarea>
          <div class="hint">one metric per line</div></div>
        <div class="field"><label>Glossary</label><textarea name='glossary' rows='5'>{glossary_text}</textarea>
          <div class="hint">one "term: definition" per line</div></div>
      </div>
    </div>
    """
    action = "/new" if is_new else f"/edit/{html.escape(key)}"
    delete_btn = (
        f"<button class='danger' formaction='/delete/{html.escape(key)}' formmethod='post' "
        "onclick=\"return confirm('Delete this dashboard entry?')\">Delete</button>"
        if not is_new
        else ""
    )
    breadcrumb = '<div class="breadcrumb"><a class="btn ghost" href="/">← All dashboards</a></div>'
    return f"""
    {breadcrumb}
    <form method="post" action="{action}">
      {basic_info}
      {content}
      {metrics}
      <div class="actions">
        <button class="primary" type="submit">Save</button>
        <a class="btn" href="/">Cancel</a>
        {delete_btn}
      </div>
    </form>
    """


# ------------------------------------------------------------------ routes

async def _index(_request: Request) -> HTMLResponse:
    return _render("Catalog admin", _list_body(_load()))


async def _new_get(_request: Request) -> HTMLResponse:
    return _render("New dashboard", _form_body({}, "", is_new=True))


async def _edit_get(request: Request) -> HTMLResponse:
    key = request.path_params["key"]
    entry = _find(_load(), key)
    if entry is None:
        return _render("Not found", f"<p>No dashboard with slug/luid '{html.escape(key)}'.</p><a href='/'>Back</a>")
    return _render(f"Edit — {entry.get('name') or key}", _form_body(entry, key, is_new=False))


def _apply_form(entry: CommentedMap, form: dict[str, str]) -> None:
    for f in FIELDS_TEXT:
        v = (form.get(f) or "").strip()
        if v:
            entry[f] = v
        elif f in entry:
            del entry[f]
    for f in FIELDS_BLOCK:
        v = (form.get(f) or "").rstrip()
        if v:
            entry[f] = LiteralScalarString(v + "\n")
        elif f in entry:
            del entry[f]
    kpis = _text_to_kpis(form.get("kpis") or "")
    if kpis:
        entry["kpis"] = kpis
    elif "kpis" in entry:
        del entry["kpis"]
    glossary = _text_to_glossary(form.get("glossary") or "")
    if glossary:
        entry["glossary"] = glossary
    elif "glossary" in entry:
        del entry["glossary"]


async def _edit_post(request: Request) -> RedirectResponse:
    key = request.path_params["key"]
    form = dict((await request.form()).items())
    data = _load()
    entry = _find(data, key)
    if entry is None:
        return RedirectResponse("/", status_code=303)
    _apply_form(entry, form)
    _save(data)
    return RedirectResponse("/", status_code=303)


async def _new_post(request: Request) -> Any:
    form = dict((await request.form()).items())
    data = _load()
    new_key = (form.get("slug") or form.get("luid") or "").strip()
    if not new_key:
        return _render("New dashboard", "<p>slug or luid is required.</p>" + _form_body(form, "", is_new=True))
    if _find(data, new_key) is not None:
        return _render(
            "New dashboard",
            "<p>An entry with that slug/luid already exists.</p>" + _form_body(form, "", is_new=True),
        )
    entry = CommentedMap()
    _apply_form(entry, form)
    data["dashboards"].append(entry)
    _save(data)
    return RedirectResponse("/", status_code=303)


async def _delete_post(request: Request) -> RedirectResponse:
    key = request.path_params["key"]
    data = _load()
    dashboards = data.get("dashboards", [])
    for i, entry in enumerate(dashboards):
        if _entry_key(entry) == key:
            del dashboards[i]
            break
    _save(data)
    return RedirectResponse("/", status_code=303)


class _BasicAuth:
    """Hand-rolled — pulling in a whole auth dependency for one guarded page isn't worth it."""

    def __init__(self, app: Any, user: str, password: str):
        self.app = app
        self.user = user
        self.password = password

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        ok = False
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                user, _, pwd = decoded.partition(":")
                ok = user == self.user and pwd == self.password
            except Exception:
                ok = False
        if not ok:
            response = PlainTextResponse(
                "Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="dashboard-context admin"'},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app() -> Any:
    app = Starlette(
        routes=[
            Route("/", _index, methods=["GET"]),
            Route("/new", _new_get, methods=["GET"]),
            Route("/new", _new_post, methods=["POST"]),
            Route("/edit/{key}", _edit_get, methods=["GET"]),
            Route("/edit/{key}", _edit_post, methods=["POST"]),
            Route("/delete/{key}", _delete_post, methods=["POST"]),
        ]
    )
    user = os.environ.get("ADMIN_USER", "admin")
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("ADMIN_PASSWORD must be set — refusing to run the catalog admin without auth.")
    return _BasicAuth(app, user, password)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    port = int(os.environ.get("HTTP_PORT", "8010"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
