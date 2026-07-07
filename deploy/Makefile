SHELL := /bin/bash

.PHONY: up down restart logs ps register health mcp-list clean

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

clean:  ## Nuke volumes too.
	docker compose down -v
