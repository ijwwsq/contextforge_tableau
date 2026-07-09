"""Print a long-lived admin JWT for MCP clients (Claude Desktop, curl, etc.).

Same shape as the bootstrap job's registration token — `is_admin=true` +
`teams=null` gives the admin-bypass path documented in ContextForge's
`normalize_token_teams()`. Signed with the same `JWT_SECRET_KEY` the gateway
uses, so it validates against the running deployment.

Usage:
    docker compose run --rm --entrypoint python bootstrap /app/mint_token.py

Env (all inherited from the compose file):
    JWT_SECRET_KEY, JWT_ALGORITHM, JWT_AUDIENCE, JWT_ISSUER
    ADMIN_EMAIL              (defaults to admin@example.com)
    TOKEN_EXPIRY_SECONDS     (defaults to 30 days)
"""
from __future__ import annotations

import os

from register import mint_admin_token


def main() -> None:
    ttl = int(os.environ.get("TOKEN_EXPIRY_SECONDS", 60 * 60 * 24 * 30))
    print(mint_admin_token(expiry_seconds=ttl))


if __name__ == "__main__":
    main()
