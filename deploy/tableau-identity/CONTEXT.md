# tableau-identity — контекст реализации (для дальнейшей работы)

Инженерный лог: что сделано, как устроено, какие решения и почему, что проверено
и что осталось. User-facing описание — [../TABLEAU-IDENTITY.md](../TABLEAU-IDENTITY.md),
схемы — [DIAGRAMS.md](DIAGRAMS.md).

---

## 1. Задача

По ТС (5.4.2): каждый ИИ-пользователь должен ходить в Tableau **под своей личной
учётной записью (персональным PAT)**, наследуя её права (RLS), а не под одним
общим техническим PAT. Управление сопоставлением — через админку (5.6.2), PAT
хранятся зашифрованными (5.5.4).

**Почему нельзя «просто передать PAT в tableau-mcp»:** официальный tableau-mcp не
принимает сырой PAT per-request. Но умеет **passthrough** — заголовок
`x-tableau-auth` с живым session-token. Session-token получают, залогинившись
персональным PAT (`POST /api/.../auth/signin`). Между ними и встаёт наш брокер.

---

## 2. Что реализовано (файлы)

Новый сервис `deploy/tableau-identity/` — образ `local/tableau-identity:0.1.0`,
два контейнера из него:

| Файл | Ответственность |
|---|---|
| `src/tableau_identity/store.py` | Хранилище привязок `user → {tableau_username, pat_name, pat_secret}`. Шифрование PAT (Fernet), инвариант 1:1, reload по mtime, атомарная запись. |
| `src/tableau_identity/tableau_auth.py` | Обмен PAT → session-token (signin/signout по докам Tableau). Кэш сессий с **пер-юзерным `asyncio.Lock`**, `invalidate()`. TLS для on-prem Server. |
| `src/tableau_identity/identity.py` | Извлечение личности из gateway-JWT: проверка подписи, claim `sub`. |
| `src/tableau_identity/broker.py` | ASGI-прокси: личность → сессия → инъекция `x-tableau-auth` → стриминг в tableau-mcp. Гейтинг `tools/call`, retry на 401. |
| `src/tableau_identity/admin.py` | Веб-админка 1:1-маппинга (UI + REST), Basic-auth. Шифрует PAT, наружу не отдаёт. |

Провижининг пользователей (пункт 1.1):
| Файл | Что |
|---|---|
| `../bootstrap/provision_user.py` | Боевой провижининг: создать юзера в гейте + (опц.) роль + личный API-токен — через реальные эндпоинты ContextForge. |
| `../bootstrap/register.py` | +`mint_user_token` (DEV-only, форженый), +`is_admin` в `mint_admin_token`. |

Тесты: `../tests/test_tableau_identity.py` (сервис), дополнения в
`../tests/test_bootstrap.py` (провижининг). Всего **108 тестов**.

---

## 3. Ключевые решения (и чем подтверждены)

- **Один PAT = одна сессия.** Docs Tableau (Server и Cloud): повторный signin тем
  же PAT убивает предыдущую сессию. → session-token кэшируется на юзера,
  переиспользуется; signin под пер-юзерным локом (иначе параллельные запросы
  одного юзера убьют сессии друг другу); signout после запроса НЕ делаем.
- **Протухание — по 401** от passthrough tableau-mcp: инвалидируем кэш, релогин,
  retry один раз.
- **Секреты зашифрованы** (Fernet, ключ `TABLEAU_IDENTITY_ENC_KEY` вне данных).
  Админ-API отдаёт только `user/tableau_username/pat_name`.
- **Непривязанный юзер** на `tools/call` → чистая JSON-RPC ошибка, до общего PAT
  запрос не доходит (1:1 строгий). Хендшейк/`tools/list` пропускаются без токена.
- **Личность = email.** Личный API-токен гейта (`POST /tokens`, `token_use=api`)
  несёт `sub = email`, подпись HS256 ключом `JWT_SECRET_KEY` → брокер
  (`IDENTITY_CLAIM=sub`, тот же секрет) читает email **без изменений**.
  ⚠️ login-токен (`/auth/email/login`) несёт `sub = UUID` — для маппинга НЕ годится.
- **Шов клиент→гейт→брокер:** клиент дублирует токен в `X-Upstream-Authorization`;
  гейт (`compute_passthrough_headers_cached`, ContextForge v1.0.4) безусловно
  переименовывает его в `Authorization` для upstream. Проверено **исполнением
  настоящей функции гейта** против кода брокера (тест
  `test_seam_real_gateway_forwards_identity_to_broker`).
- **Tableau Server:** REST-логин по PAT идентичен Cloud; отличие — только конфиг
  (`TABLEAU_SERVER`, `TABLEAU_API_VERSION` пинится к релизу Server,
  `TABLEAU_SSL_VERIFY` для self-signed сертификата).

---

## 4. Как всё связано (docker-compose)

- `tableau-identity` (брокер) регистрируется в гейте как сервер `tableau` (вместо
  прямого tableau-mcp) — правка в `bootstrap` env `TABLEAU_MCP_URL`.
- `tableau-identity-admin` — админка маппинга, порт `${IDENTITY_ADMIN_PORT:-8021}`,
  том `catalog` rw (брокер читает ro).
- `tableau-mcp` — добавлен `ENABLE_PASSTHROUGH_AUTH=true`.
- `redis` — снята хост-публикация порта (override, чтобы не конфликтовать).

**Env (полный список — в `.env.example`):** `TABLEAU_IDENTITY_ENC_KEY`,
`IDENTITY_ADMIN_USER/PASSWORD/PORT`, `IDENTITY_CLAIM`, `TABLEAU_SERVER`,
`TABLEAU_API_VERSION`, `TABLEAU_SITE_NAME`, `TABLEAU_SSL_VERIFY`,
`TABLEAU_SESSION_TTL_SECONDS`.

---

## 5. Операторский цикл (на одного пользователя)

```bash
# 0) однократно: ключ шифрования PAT в .env
make identity-key                      # → TABLEAU_IDENTITY_ENC_KEY=...

# 1) завести юзера в гейте + выпустить его личный API-токен (+сниппет Claude)
make provision-user EMAIL=alice@corp PASSWORD=Secret123 ROLE=developer DAYS=90

# 2) закинуть его PAT в tableau-identity (секрет вводится скрыто)
make map-pat EMAIL=alice@corp TABLEAU_USER=alice@corp PAT_NAME=alice-mcp

# 3) вставить сниппет из шага 1 в claude_desktop_config.json
```
Ключ связки — **email**: одинаковый в шагах 1 и 2 (он же `sub` в токене).
PAT можно завести и руками в UI: `http://localhost:8021/` (Basic-auth).

**Запуск без тяжёлого гейта (для отладки брокера):**
`make up-identity` — поднимает только `tableau-identity` + `admin` + `tableau-mcp`.

---

## 6. Что проверено

- **108 юнит-тестов** (store, tableau_auth, identity, broker, admin, provisioning,
  TLS, seam) — зелёные. Запуск: `make test`.
- **Живой HTTP-стенд** брокера 9/9 (реальные брокер+админка+фейковые Tableau и
  tableau-mcp).
- **Шов с гейтом** — исполнением настоящей функции ContextForge v1.0.4.
- **Docker-образ** собирается, контейнеры поднимаются healthy, админка отвечает,
  заливка PAT через UI/curl шифрует секрет на диске.
- **НЕ гонялось живьём:** полный e2e через поднятый гейт до боевого Tableau —
  тяжёлый стек роняет машину по OMM, поэтому не запускался (шов при этом доказан
  кодом гейта, обмен PAT→сессия — на стенде).

---

## 7. Что осталось (роадмап)

1. **1.2 — 4 роли RBAC ТС** (Администратор платформы / Администратор доступа /
   Бизнес-аналитик / Конечный пользователь). Встроенные роли гейта:
   `platform_admin/team_admin/developer/viewer` + гранулярные права + скоуп по
   командам. Без роли с правом вызова инструментов не-admin юзер к серверу
   `tableau` не пройдёт. `provision_user` уже принимает `ROLE=`.
2. **1.4 — доступ к админкам** (`tableau-identity-admin`,
   `dashboard-context-admin`) в единой RBAC-модели: сейчас отдельный Basic-auth,
   наружу через nginx не проброшены. Решить: проброс через гейт vs Basic-auth по
   ролям.
3. **Версионирование бизнес-контекста** (ТС 4.5 / 5.6.3) — сейчас last-write-wins
   YAML без истории.
4. **6 инструментов Tableau** (5.4.3) — сверить имена, ограничить `INCLUDE_TOOLS`,
   функционально протестировать (часть требует Server 2024.2+).
5. **Keycloak / SSO** (ТС 5.5) — ContextForge поддерживает нативно
   (`sso_keycloak_enabled` + маппинг ролей); брокер к этому готов, меняется только
   источник токена. Отнесено на «потом».
6. **Аудит** (флаги уже проброшены в compose) — включить + согласовать retention.
7. **Живая приёмка e2e** на машине с достаточной RAM.

---

## 8. Важное ограничение среды

Полный стек (`make up`: гейт ×3 + postgres + redis + pgbouncer + nginx + MCP-сервисы)
**роняет машину разработчика по OOM**. Для проверки — юнит-тесты, лёгкий
`make up-identity`, одиночный `docker build`. Полный стек — только на машине с
достаточной RAM. У контейнеров `restart: unless-stopped` — после падения демона
они воскресают; лечится `make down`.
