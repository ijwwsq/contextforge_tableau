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


def mint_admin_token(expiry_seconds: int = 3600, subject: str | None = None) -> str:
    """Mint an admin JWT accepted by ContextForge.

    Args:
        expiry_seconds: TTL for the token. Bootstrap uses the short default;
            client-facing tools like `mint_token.py` pass a longer value.
        subject: Overrides the `sub`/`email` claim. Defaults to ADMIN_EMAIL.
    """
    secret = os.environ["JWT_SECRET_KEY"]
    algo = os.environ.get("JWT_ALGORITHM", "HS256")
    sub = subject or os.environ.get("ADMIN_EMAIL", "admin@example.com")
    now = int(time.time())
    payload = {
        "sub": sub,
        "email": sub,
        "aud": os.environ.get("JWT_AUDIENCE", "mcpgateway-api"),
        "iss": os.environ.get("JWT_ISSUER", "mcpgateway"),
        "is_admin": True,
        "teams": None,          # admin bypass — see mcp-context-forge CLAUDE.md
        "iat": now,
        "exp": now + expiry_seconds,
        "jti": f"bootstrap-{now}",
    }
    return jwt.encode(payload, secret, algorithm=algo)


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
