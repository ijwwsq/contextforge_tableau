"""Создание 4 ролей RBAC из ТС (пункт 1.2) в ContextForge.

Идемпотентно заводит роли least-privilege под роли ТС 5.5.3 через реальный
`POST /rbac/roles` (существующие по имени пропускает). Права — из констант
`Permissions` гейта. Все роли global-scope (у нас single-tenant, без команд).

Зачем свои, а не встроенные: встроенный `developer` даёт конечному юзеру ещё и
create/update/delete на серверах и инструментах — он мог бы удалять/регать
инструменты. Для «Конечного пользователя» нужно только read+execute.

Роли (name → назначение ТС):
  mcp-platform-admin  — Администратор платформы (MCP-серверов)
  mcp-access-admin    — Администратор пользователей и доступа
  mcp-analyst         — Бизнес-аналитик
  mcp-user            — Конечный пользователь (ИИ-агент): только вызов инструментов

Env: GATEWAY_URL, JWT_SECRET_KEY (+ algo/aud/iss), ADMIN_EMAIL.
"""
from __future__ import annotations

import os
import sys

import httpx

from register import mint_admin_token

ROLES: list[dict[str, object]] = [
    {
        "name": "mcp-platform-admin",
        "description": "Администратор платформы (MCP-серверов): серверы, инструменты, мониторинг.",
        "scope": "global",
        "permissions": [
            "gateways.create", "gateways.read", "gateways.update", "gateways.delete",
            "servers.create", "servers.read", "servers.use", "servers.update", "servers.delete", "servers.manage",
            "tools.create", "tools.read", "tools.update", "tools.delete", "tools.execute", "tools.manage_plugins",
            "resources.create", "resources.read", "resources.update", "resources.delete",
            "prompts.create", "prompts.read", "prompts.update", "prompts.delete", "prompts.execute",
            "admin.overview", "admin.dashboard", "admin.metrics", "admin.events",
            "admin.plugins", "admin.import", "admin.export",
        ],
    },
    {
        "name": "mcp-access-admin",
        "description": "Администратор пользователей и доступа: юзеры, роли, токены.",
        "scope": "global",
        "permissions": [
            "users.create", "users.read", "users.update", "users.delete", "users.invite",
            "teams.create", "teams.read", "teams.update", "teams.delete", "teams.manage_members",
            "tokens.create", "tokens.read", "tokens.update", "tokens.revoke",
            "admin.user_management", "admin.security_audit", "admin.overview", "admin.dashboard", "admin.events",
        ],
    },
    {
        "name": "mcp-analyst",
        "description": "Бизнес-аналитик: чтение каталога + вызов инструментов контекста.",
        "scope": "global",
        "permissions": [
            "gateways.read", "servers.read", "servers.use",
            "tools.read", "tools.execute", "resources.read", "prompts.read", "prompts.execute",
            "tokens.create", "tokens.read", "admin.overview",
        ],
    },
    {
        "name": "mcp-user",
        "description": "Конечный пользователь (ИИ-агент): только вызов доступных инструментов.",
        "scope": "global",
        "permissions": [
            "servers.read", "servers.use",
            "tools.read", "tools.execute",
            "resources.read", "prompts.read", "prompts.execute",
            "tokens.create", "tokens.read",
        ],
    },
]


def existing_role_names(client: httpx.Client, base: str, h: dict[str, str]) -> set[str]:
    r = client.get(f"{base}/rbac/roles", headers=h)
    r.raise_for_status()
    return {role.get("name") for role in r.json()}


def create_role(client: httpx.Client, base: str, h: dict[str, str], role: dict[str, object]) -> str:
    r = client.post(f"{base}/rbac/roles", headers=h, json=role)
    if r.status_code in (200, 201):
        return "created"
    if r.status_code == 409:
        return "exists"
    raise SystemExit(f"роль {role['name']}: {r.status_code} {r.text}")


def main() -> None:
    base = os.environ["GATEWAY_URL"].rstrip("/")
    h = {"Authorization": f"Bearer {mint_admin_token()}", "Content-Type": "application/json"}
    with httpx.Client(timeout=15.0) as client:
        have = existing_role_names(client, base, h)
        for role in ROLES:
            if role["name"] in have:
                print(f"[=] роль уже есть: {role['name']}")
                continue
            status = create_role(client, base, h, role)
            print(f"[ok] роль {status}: {role['name']}")
    print("роли ТС готовы. Назначение — make provision-user ROLE=mcp-user", file=sys.stderr)


if __name__ == "__main__":
    main()
