SHELL := /bin/bash

.PHONY: up down restart logs ps register health mcp-list token claude-config clean

up:  ## Bring up the full stack (gateway + tableau-mcp + dashboard-context + bootstrap).
	docker compose up -d --build

down:  ## Stop and remove containers (volumes preserved).
	docker compose down

restart: down up

logs:  ## Tail all logs.
	docker compose logs -f --tail=200

ps:
	docker compose ps

register:  ## Re-run the bootstrap registration (idempotent).
	docker compose run --rm bootstrap

health:  ## Curl the gateway health endpoint.
	curl -sf http://localhost:$${NGINX_PORT:-8080}/health && echo

mcp-list:  ## Show federated gateways registered in ContextForge.
	docker compose exec gateway curl -sf \
	  -H "Authorization: Bearer $$(docker compose run --rm bootstrap python -c 'from register import mint_admin_token; print(mint_admin_token())')" \
	  http://localhost:4444/gateways

token:  ## Mint a long-lived admin JWT for MCP clients (override TOKEN_EXPIRY_SECONDS; 30d default).
	@docker compose run --rm --no-deps -e TOKEN_EXPIRY_SECONDS --entrypoint python bootstrap /app/mint_token.py

claude-config:  ## Print a ready-to-paste claude_desktop_config.json snippet.
	@TOKEN="$$(docker compose run --rm --no-deps -e TOKEN_EXPIRY_SECONDS --entrypoint python bootstrap /app/mint_token.py 2>/dev/null | tr -d '\r\n')"; \
	URL="$${MCP_PUBLIC_URL:-http://localhost:$${NGINX_PORT:-8080}/mcp}"; \
	printf '{\n  "mcpServers": {\n    "tableau-gateway": {\n      "command": "npx",\n      "args": [\n        "-y",\n        "mcp-remote",\n        "%s",\n        "--header",\n        "Authorization:Bearer %s"\n      ]\n    }\n  }\n}\n' "$$URL" "$$TOKEN"

clean:  ## Nuke volumes too.
	docker compose down -v
