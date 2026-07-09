# Tableau MCP gateway — first iteration

Stack: **ContextForge** (no Keycloak) fronting two federated MCP servers:

- **`tableau`** — official Tableau MCP (`github.com/tableau/tableau-mcp`) in streamable-HTTP mode.
- **`dashboard-context`** — our custom MCP that merges Tableau REST metadata with a versioned business catalog.

```
Claude Desktop ──▶ nginx :8080 ──▶ ContextForge gateway ──▶ ┬─▶ tableau-mcp :3927/tableau-mcp
                                                            └─▶ dashboard-context :8000/mcp
```

## Layout

```
deploy/
├── docker-compose.yml                    # includes ../mcp-context-forge/docker-compose.yml + our 3 services
├── .env.example                          # secrets template
├── Makefile                              # up / down / logs / register / token / claude-config
├── claude-desktop-config.example.json    # snippet operators drop into Claude Desktop's config
├── tableau-mcp/
│   └── Dockerfile                        # builds tableau/tableau-mcp @ TABLEAU_MCP_REF
├── dashboard-context/                    # custom MCP server
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── src/dashboard_context/{server,catalog,tableau}.py
│   └── catalog/dashboards.yml            # human-authored business context (edit in PRs)
└── bootstrap/
    ├── Dockerfile
    ├── register.py                       # mints admin JWT, POSTs both MCPs to /gateways
    └── mint_token.py                     # prints a long-lived admin JWT for `make token` / `make claude-config`
```

## Bring it up

```bash
cp .env.example .env
# fill in JWT_SECRET_KEY, POSTGRES_PASSWORD, PLATFORM_ADMIN_PASSWORD,
# TABLEAU_SERVER / TABLEAU_PAT_NAME / TABLEAU_PAT_VALUE
make up
make logs           # watch bootstrap register both MCPs
```

Admin UI: <http://localhost:8080/admin> — log in as `PLATFORM_ADMIN_EMAIL` / `PLATFORM_ADMIN_PASSWORD`.

## Connect Claude Desktop

The gateway exposes the aggregate MCP endpoint at `http://localhost:8080/mcp`
(streamable HTTP, Bearer auth). Claude Desktop only speaks stdio natively, so
we bridge with [`mcp-remote`](https://www.npmjs.com/package/mcp-remote).

1. Mint a token signed with the same `JWT_SECRET_KEY` the gateway uses:

   ```bash
   make token          # prints a 30-day admin JWT
   ```

2. Print a ready-to-paste `claude_desktop_config.json` fragment with that
   token embedded:

   ```bash
   make claude-config
   ```

3. Merge it into your Claude Desktop config
   (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
   `%APPDATA%\Claude\claude_desktop_config.json` on Windows) — see
   `claude-desktop-config.example.json` for the shape — and restart Claude
   Desktop. The `tableau-gateway` server should appear with tools from both
   `tableau` and `dashboard-context` federated behind it.

Node ≥ 18 is required for `npx mcp-remote`. If Claude Desktop is exposed to
a remote deployment, override the URL via `MCP_PUBLIC_URL=https://your-host/mcp make claude-config`.

## What the custom tool does

`dashboard-context` exposes two tools:

- `list_dashboards()` — every entry in `catalog/dashboards.yml`.
- `describe_dashboard(dashboard_id)` — takes a Tableau view LUID or a catalog slug, returns:
  - live Tableau metadata (title, project, workbook, updated_at)
  - business context from the YAML catalog (purpose, owner, KPIs, freshness SLA, glossary)
  - a `sources` flag so the caller knows which halves succeeded

The idea: **Tableau knows the shape of the dashboard; humans know what it means.** The model gets both in one response and can write a description a business user actually wants to read.

Edit `catalog/dashboards.yml` under version control — it's mounted read-only into the container and reloads on mtime change, so iteration doesn't need a rebuild.

## Prod migration checklist (what changes)

- Rotate `JWT_SECRET_KEY`, `POSTGRES_PASSWORD`, `PLATFORM_ADMIN_PASSWORD` to real secret-manager values.
- Swap `TABLEAU_PAT_*` to whatever your prod path is (still PAT for MVP; per-user OAuth is the next iteration — see `ARCHITECTURE.md §5`).
- Point `APP_DOMAIN` at your real hostname; put nginx behind your real TLS terminator or turn on the `tls` profile.
- Publish `local/tableau-mcp`, `local/dashboard-context`, `local/mcp-bootstrap` to your registry with immutable tags and switch `image:` refs.
- Move `catalog/dashboards.yml` to its own repo or a ConfigMap.
- When you're ready for SSO: add `--profile sso` and follow `../mcp-context-forge/docker-compose.sso.yml`. Keycloak stays off in this iteration.

## Common ops

```bash
make ps
make logs
make register           # re-run bootstrap (idempotent)
make token              # mint a fresh admin JWT for MCP clients
make claude-config      # print a Claude Desktop config snippet with a fresh token
make down               # stop; volumes preserved
make clean              # stop + wipe volumes
```
