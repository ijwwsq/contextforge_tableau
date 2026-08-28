"""Боевой провижининг пользователя в ContextForge (пункт 1.1).

Регистрирует НАСТОЯЩЕГО пользователя гейта и выпускает ему личный API-токен —
через реальные эндпоинты ContextForge, без форженых JWT. Токен несёт `sub =
email` (проверено: `token_use=api`, подпись HS256 ключом JWT_SECRET_KEY), тот же
email — ключ привязки в tableau-identity.

Поток (админ-права берём из mint_admin_token — это платформенный админ):
  1. POST /auth/email/admin/users        — создать пользователя (email+пароль)
  2. POST /rbac/users/{email}/roles       — (опц.) назначить роль по имени
  3. POST /tokens (user_email=<email>)    — выпустить личный API-токен (sub=email)

Печатает токен и готовый сниппет для Claude Desktop.

Env:
  GATEWAY_URL, JWT_SECRET_KEY (+ JWT_ALGORITHM/AUDIENCE/ISSUER), ADMIN_EMAIL
  USER_EMAIL, USER_PASSWORD              — кого заводим (обязательны)
  USER_ROLE                              — имя роли RBAC (опц.; scope global)
  TOKEN_NAME                             — имя токена (по умолчанию '<email> MCP')
  TOKEN_EXPIRES_IN_DAYS                  — TTL токена в днях (опц.; пусто = бессрочный)
  MCP_PUBLIC_URL                         — URL /mcp для сниппета (опц.)
"""
from __future__ import annotations

import os
import sys

import httpx

from register import mint_admin_token


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {mint_admin_token()}", "Content-Type": "application/json"}


def create_user(client: httpx.Client, base: str, h: dict[str, str], email: str, password: str) -> None:
    r = client.post(
        f"{base}/auth/email/admin/users",
        headers=h,
        json={"email": email, "password": password, "is_admin": False, "is_active": True},
    )
    if r.status_code in (200, 201):
        print(f"[ok] пользователь создан: {email}")
        return
    if r.status_code == 409 or (r.status_code == 400 and "exist" in r.text.lower()):
        print(f"[=] пользователь уже есть: {email}")
        return
    raise SystemExit(f"не удалось создать пользователя {email}: {r.status_code} {r.text}")


def assign_role(client: httpx.Client, base: str, h: dict[str, str], email: str, role_name: str) -> None:
    roles = client.get(f"{base}/rbac/roles", headers=h)
    roles.raise_for_status()
    match = next((role for role in roles.json() if role.get("name") == role_name), None)
    if match is None:
        names = ", ".join(sorted(r.get("name", "?") for r in roles.json())) or "(пусто)"
        raise SystemExit(f"роль '{role_name}' не найдена. Доступные: {names}")
    r = client.post(
        f"{base}/rbac/users/{email}/roles",
        headers=h,
        json={"role_id": match["id"], "scope": "global", "scope_id": None},
    )
    if r.status_code in (200, 201):
        print(f"[ok] роль назначена: {role_name} → {email}")
    elif r.status_code == 409:
        print(f"[=] роль уже назначена: {role_name} → {email}")
    else:
        raise SystemExit(f"не удалось назначить роль: {r.status_code} {r.text}")


def issue_token(client: httpx.Client, base: str, h: dict[str, str], email: str) -> str:
    body: dict[str, object] = {
        "name": os.environ.get("TOKEN_NAME") or f"{email} MCP token",
        "user_email": email,  # admin-делегация: выпускаем токен ЗА пользователя
    }
    days = os.environ.get("TOKEN_EXPIRES_IN_DAYS", "").strip()
    if days:
        body["expires_in_days"] = int(days)
    r = client.post(f"{base}/tokens", headers=h, json=body)
    if r.status_code not in (200, 201):
        raise SystemExit(f"не удалось выпустить токен: {r.status_code} {r.text}")
    token = r.json().get("access_token")
    if not token:
        raise SystemExit(f"в ответе нет access_token: {r.text}")
    return token


def main() -> None:
    base = os.environ["GATEWAY_URL"].rstrip("/")
    email = os.environ.get("USER_EMAIL", "").strip()
    password = os.environ.get("USER_PASSWORD", "").strip()
    if not email or not password:
        raise SystemExit("USER_EMAIL и USER_PASSWORD обязательны.")
    role = os.environ.get("USER_ROLE", "").strip()

    h = _admin_headers()
    with httpx.Client(timeout=15.0) as client:
        create_user(client, base, h, email, password)
        if role:
            assign_role(client, base, h, email, role)
        token = issue_token(client, base, h, email)

    mcp_url = os.environ.get("MCP_PUBLIC_URL", "http://localhost:8080/mcp")
    print("\n─── личный токен (sub=" + email + ") ───")
    print(token)
    print("\n─── сниппет для claude_desktop_config.json ───")
    print(
        '{\n  "mcpServers": {\n    "tableau-gateway": {\n      "command": "npx",\n'
        '      "args": ["-y", "mcp-remote", "' + mcp_url + '",\n'
        '        "--header", "Authorization:Bearer ' + token + '",\n'
        '        "--header", "X-Upstream-Authorization:Bearer ' + token + '"]\n'
        "    }\n  }\n}"
    )
    print(
        "\nНе забудь завести привязку этого email в tableau-identity-admin "
        "(http://localhost:8021/): " + email + " → учётка Tableau + PAT.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
