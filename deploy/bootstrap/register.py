"""Idempotent registration of federated MCP servers into ContextForge.

Runs once after the gateway is healthy. Mints an admin JWT with the same
secret the gateway uses, then POSTs each MCP server via /gateways. Existing
entries (same name) are updated in place.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

import httpx
import jwt


def mint_admin_token(
    expiry_seconds: int = 3600, subject: str | None = None, is_admin: bool = True
) -> str:
    """Mint a JWT accepted by ContextForge.

    Args:
        expiry_seconds: TTL for the token. Bootstrap uses the short default;
            client-facing tools like `mint_token.py` pass a longer value.
            `0` (или отрицательное) → бессрочный токен (без `exp`-клейма).
        subject: Overrides the `sub`/`email` claim. Defaults to ADMIN_EMAIL.
        is_admin: `True` — admin-токен (admin bypass). `False` — per-user токен:
            его `sub` и есть личность, по которой tableau-identity находит
            привязку user→PAT. Промежуточный вариант до Keycloak (ТС 5.5).
    """
    secret = os.environ["JWT_SECRET_KEY"]
    algo = os.environ.get("JWT_ALGORITHM", "HS256")
    sub = subject or os.environ.get("ADMIN_EMAIL", "admin@example.com")
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "email": sub,
        "aud": os.environ.get("JWT_AUDIENCE", "mcpgateway-api"),
        "iss": os.environ.get("JWT_ISSUER", "mcpgateway"),
        "is_admin": is_admin,
        "teams": None,          # admin bypass при is_admin — see mcp-context-forge CLAUDE.md
        "iat": now,
        "jti": f"bootstrap-{now}",
    }
    if expiry_seconds > 0:
        # exp кладём только когда явно попросили — 0 означает "живёт вечно".
        payload["exp"] = now + expiry_seconds
    return jwt.encode(payload, secret, algorithm=algo)


def mint_user_token(subject: str, expiry_seconds: int = 0) -> str:
    """DEV-ONLY: форженый per-user токен (не-admin), `sub` = учётка юзера.

    В обход пользовательской БД и RBAC гейта — годится для локальной отладки
    брокера, но НЕ для прода. Боевой путь — provision_user.py: настоящий
    пользователь гейта + личный API-токен (тоже `sub=email`), с ролями RBAC.
    """
    return mint_admin_token(expiry_seconds=expiry_seconds, subject=subject, is_admin=False)


def wait_for_gateway(client: httpx.Client, url: str, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = client.get(f"{url}/health", timeout=3.0)
            if r.status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise SystemExit(f"gateway {url} not reachable in {timeout}s")


def register_gateway(client: httpx.Client, base: str, headers: dict[str, str], entry: dict[str, Any]) -> None:
    r = client.post(f"{base}/gateways", headers=headers, json=entry)
    if r.status_code in (200, 201):
        print(f"registered {entry['name']} -> {entry['url']}")
        return
    if r.status_code == 409:
        # Already exists — look it up and PUT to update transport/url.
        existing = client.get(f"{base}/gateways", headers=headers).json()
        for row in existing:
            if row.get("name") == entry["name"]:
                gid = row.get("id")
                upd = client.put(f"{base}/gateways/{gid}", headers=headers, json=entry)
                upd.raise_for_status()
                print(f"updated {entry['name']} -> {entry['url']}")
                return
        raise SystemExit(f"conflict on {entry['name']} but not found in listing")
    raise SystemExit(f"failed to register {entry['name']}: {r.status_code} {r.text}")


def main() -> None:
    base = os.environ["GATEWAY_URL"].rstrip("/")
    token = mint_admin_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    entries = [
        {
            "name": "tableau",
            "description": "Official Tableau MCP server (HTTP transport).",
            "url": os.environ["TABLEAU_MCP_URL"],
            "transport": "STREAMABLEHTTP",
        },
        {
            "name": "dashboard-context",
            "description": "Custom MCP: business context + Tableau metadata for dashboards.",
            "url": os.environ["DASHBOARD_CONTEXT_URL"],
            "transport": "STREAMABLEHTTP",
        },
        {
            "name": "presentation-context",
            "description": "Custom MCP: mandatory corporate presentation style guide.",
            "url": os.environ["PRESENTATION_CONTEXT_URL"],
            "transport": "STREAMABLEHTTP",
        },
    ]

    failures = 0
    with httpx.Client(timeout=10.0) as client:
        wait_for_gateway(client, base)
        for entry in entries:
            try:
                register_gateway(client, base, headers, entry)
            except SystemExit as exc:
                failures += 1
                print(f"warn: {entry['name']}: {exc}", file=sys.stderr)

    print(f"bootstrap done ({failures} failure(s))", file=sys.stderr)
    if failures == len(entries):
        raise SystemExit("all registrations failed")


if __name__ == "__main__":
    main()
