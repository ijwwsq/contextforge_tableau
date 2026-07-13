# Tableau MCP gateway — первая итерация

Стек: **ContextForge** (без Keycloak) поднимает шлюз перед двумя федеративными MCP-серверами:

- **`tableau`** — официальный Tableau MCP (`github.com/tableau/tableau-mcp`) в режиме streamable-HTTP.
- **`dashboard-context`** — наш кастомный MCP, который смешивает live-метаданные из Tableau REST с бизнес-каталогом, который отдаёт отдельный сервис-админка.

```
Claude Desktop ──▶ nginx :8080 ──▶ ContextForge gateway ──▶ ┬─▶ tableau-mcp :3927/tableau-mcp
                                                            └─▶ dashboard-context :8000/mcp
                                                                    │
                                                                    └── CATALOG_URL ──▶ dashboard-context-admin :8010/api/dashboards
```

## Раскладка

```
deploy/
├── docker-compose.yml                    # инклюдит ../mcp-context-forge/docker-compose.yml + наши сервисы
├── .env.example                          # шаблон секретов
├── Makefile                              # up / down / logs / register / token / claude-config / test
├── claude-desktop-config.example.json    # сниппет для конфига Claude Desktop
├── tableau-mcp/
│   └── Dockerfile                        # собирает tableau/tableau-mcp @ TABLEAU_MCP_REF
├── dashboard-context/                    # кастомный MCP + админка каталога
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── src/dashboard_context/{server,catalog,tableau,admin}.py
│   └── catalog/dashboards.yml            # хранилище, которым владеет админка; редактируется через веб-UI
├── bootstrap/
│   ├── Dockerfile
│   ├── register.py                       # минтит admin-JWT и POST-ит оба MCP в /gateways
│   └── mint_token.py                     # печатает долгоживущий admin-JWT для `make token` / `make claude-config`
└── tests/                                # pytest — покрывает MCP, админку и bootstrap (см. `make test`)
```

## Запуск

```bash
cp .env.example .env
# заполнить JWT_SECRET_KEY, POSTGRES_PASSWORD, PLATFORM_ADMIN_PASSWORD,
# ADMIN_PASSWORD (редактор каталога dashboard-context),
# TABLEAU_SERVER / TABLEAU_PAT_NAME / TABLEAU_PAT_VALUE
make up
make logs           # смотрим, как bootstrap регистрирует оба MCP
```

Две админ-панели:

- ContextForge admin — <http://localhost:8080/admin> — логин `PLATFORM_ADMIN_EMAIL` / `PLATFORM_ADMIN_PASSWORD`.
- Редактор каталога dashboard-context — <http://localhost:8010/> — логин `ADMIN_USER` / `ADMIN_PASSWORD` (basic auth). Владеет `catalog/dashboards.yml`; MCP-сервер тянет контекст из `http://dashboard-context-admin:8010/api/dashboards` по мере запросов. Сервис также предоставляет:
  - `GET /healthz` — docker healthcheck, без авторизации.
  - `GET /api/dashboards`, `GET /api/dashboards/{luid|slug}` — чтение (без авторизации, только для внутреннего использования).
  - `POST /api/dashboards`, `PUT /api/dashboards/{key}`, `DELETE /api/dashboards/{key}` — запись, требует basic-auth.
  - `GET /api/export` — скачать сырой YAML для бэкапа / коммита в репу.

## Подключить Claude Desktop

Шлюз отдаёт агрегированный MCP-эндпоинт на `http://localhost:8080/mcp`
(streamable HTTP, Bearer-авторизация). Claude Desktop нативно умеет только stdio,
поэтому мостим через [`mcp-remote`](https://www.npmjs.com/package/mcp-remote).

1. Минтим токен, подписанный тем же `JWT_SECRET_KEY`, что и шлюз:

   ```bash
   make token          # печатает admin-JWT на 30 дней
   ```

2. Печатаем готовый к вставке фрагмент `claude_desktop_config.json`
   с уже подставленным токеном:

   ```bash
   make claude-config
   ```

3. Мёржим его в свой конфиг Claude Desktop
   (`~/Library/Application Support/Claude/claude_desktop_config.json` на macOS,
   `%APPDATA%\Claude\claude_desktop_config.json` на Windows) — форма показана в
   `claude-desktop-config.example.json` — и перезапускаем Claude Desktop.
   Сервер `tableau-gateway` должен появиться с тулзами обоих
   `tableau` и `dashboard-context`, федерированными за ним.

Для `npx mcp-remote` нужен Node ≥ 18. Если Claude Desktop смотрит в
удалённый деплой, переопределяем URL через `MCP_PUBLIC_URL=https://your-host/mcp make claude-config`.

## Что делает кастомная тулза

`dashboard-context` отдаёт две тулзы:

- `list_dashboards()` — все записи из каталога.
- `describe_dashboard(dashboard_id)` — принимает Tableau view LUID или slug из каталога и возвращает:
  - live-метаданные из Tableau (title, project, workbook, updated_at),
  - бизнес-контекст из каталога (purpose, owner, KPIs, freshness SLA, glossary),
  - флаг `sources`, чтобы вызывающая сторона понимала, какие части ответа успешны.

Идея: **Tableau знает форму дашборда; люди знают, что он значит.** Модель получает и то, и другое одним ответом и может написать описание, которое действительно нужно бизнес-пользователю.

Каталогом владеет `dashboard-context-admin` (`:8010`): правишь записи в веб-UI, и MCP-сервер видит их на следующем запросе (кэш 5 секунд). YAML-файл остаётся представлением на диске, но MCP-сервер напрямую его больше не читает — либо задан `CATALOG_URL` (ходит в админку по HTTP), либо на локальных запусках его не задаём и падаем в fallback на `CATALOG_PATH`.

## Тесты

Полное покрытие (`dashboard-context`, `bootstrap`, интеграция MCP↔админка) лежит в `deploy/tests/` и гоняется через:

```bash
make test           # cd tests && python3 -m pytest -v
```

Ожидаемо: 72 теста зелёные. Зависимости для тестов:
`pytest pytest-asyncio httpx pyyaml starlette ruamel.yaml mcp python-multipart pyjwt uvicorn`.

## Чеклист миграции в прод (что меняется)

- Прокрутить `JWT_SECRET_KEY`, `POSTGRES_PASSWORD`, `PLATFORM_ADMIN_PASSWORD` — подставить реальные значения из секрет-менеджера.
- Поменять `TABLEAU_PAT_*` на тот путь, что используется в проде (для MVP всё ещё PAT; per-user OAuth — следующая итерация, см. `ARCHITECTURE.md §5`).
- Указать `APP_DOMAIN` на реальный хостнейм; поставить nginx за настоящий TLS-терминатор или включить профиль `tls`.
- Опубликовать `local/tableau-mcp`, `local/dashboard-context`, `local/mcp-bootstrap` в регистри с иммутабельными тегами и переключить `image:` на них.
- Перенести `catalog/dashboards.yml` на persistent volume — либо заменить `dashboard-context-admin` на сервис с БД (MCP-серверу достаточно любого, кто отдаёт `/api/dashboards`).
- Прокрутить `ADMIN_PASSWORD` и подумать про SSO перед редактором каталога вместо встроенного basic-auth.
- Когда доходим до SSO: добавляем `--profile sso` и следуем `../mcp-context-forge/docker-compose.sso.yml`. Keycloak в этой итерации выключен.

## Часто нужные команды

```bash
make ps
make logs
make register           # перезапустить bootstrap (идемпотентно)
make token              # свежий admin-JWT для MCP-клиентов
make claude-config      # напечатать сниппет конфига Claude Desktop со свежим токеном
make test               # прогнать pytest по всему стеку
make down               # остановить; volumes сохраняются
make clean              # остановить + снести volumes
```
