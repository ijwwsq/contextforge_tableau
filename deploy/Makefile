SHELL := /bin/bash

.PHONY: up up-identity down down-heavy restart logs ps register health mcp-list token claude-config clean test test-deps identity-key user-token provision-user provision-roles map-pat

up:  ## Bring up the full stack (gateway + Tableau, dashboard and presentation context + bootstrap).
	docker compose up -d --build

up-identity:  ## ЛЁГКИЙ режим: снести тяжёлый стек и поднять ТОЛЬКО tableau-identity (+admin +tableau-mcp). Без гейта/БД/redis, без сборки/пуллов.
	docker compose down --remove-orphans
	docker compose up -d --no-build tableau-identity tableau-identity-admin
	@echo "tableau-identity (broker :internal) + admin: http://localhost:$${IDENTITY_ADMIN_PORT:-8021}/"

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

token:  ## Mint an admin JWT for MCP clients (default: no exp — never expires; TOKEN_EXPIRY_SECONDS=N to set TTL in seconds).
	@docker compose run --rm --no-deps -e TOKEN_EXPIRY_SECONDS --entrypoint python bootstrap /app/mint_token.py

provision-roles:  ## Создать 4 роли RBAC из ТС (idempotent): mcp-platform-admin/access-admin/analyst/user.
	@docker compose run --rm --no-deps --entrypoint python bootstrap /app/provision_roles.py

provision-user:  ## БОЕВОЙ: завести юзера в гейте + выпустить его личный API-токен. make provision-user EMAIL=alice@corp PASSWORD=secret [ROLE=mcp-user] [DAYS=90]
	@test -n "$(EMAIL)" -a -n "$(PASSWORD)" || { echo "нужны EMAIL и PASSWORD, напр. make provision-user EMAIL=alice@corp PASSWORD=secret"; exit 1; }
	@docker compose run --rm --no-deps \
	  -e USER_EMAIL="$(EMAIL)" -e USER_PASSWORD="$(PASSWORD)" -e USER_ROLE="$(ROLE)" -e TOKEN_EXPIRES_IN_DAYS="$(DAYS)" \
	  --entrypoint python bootstrap /app/provision_user.py

map-pat:  ## Завести привязку user→Tableau PAT (секрет спросит скрыто). make map-pat EMAIL=alice@corp TABLEAU_USER=alice@corp PAT_NAME=alice-mcp
	@test -n "$(EMAIL)" -a -n "$(TABLEAU_USER)" -a -n "$(PAT_NAME)" || { echo "нужны EMAIL, TABLEAU_USER, PAT_NAME"; exit 1; }
	@set -a; [ -f .env ] && . ./.env; set +a; \
	 read -rs -p "PAT secret для $(EMAIL): " S; echo; \
	 curl -sf -u "$${IDENTITY_ADMIN_USER:-admin}:$${IDENTITY_ADMIN_PASSWORD}" \
	   --data-urlencode "user=$(EMAIL)" --data-urlencode "tableau_username=$(TABLEAU_USER)" \
	   --data-urlencode "pat_name=$(PAT_NAME)" --data-urlencode "pat_secret=$$S" \
	   -o /dev/null -w "map-pat: HTTP %{http_code} (303 = ок)\n" \
	   "http://localhost:$${IDENTITY_ADMIN_PORT:-8021}/"

user-token:  ## DEV-ONLY: форженый per-user JWT (в обход БД/RBAC гейта). Для прода: provision-user. make user-token EMAIL=alice@corp
	@test -n "$(EMAIL)" || { echo "EMAIL=<user> обязателен"; exit 1; }
	@docker compose run --rm --no-deps --entrypoint python bootstrap -c \
	  "from register import mint_user_token; print(mint_user_token('$(EMAIL)'))"

claude-config:  ## Print a ready-to-paste claude_desktop_config.json snippet.
	@TOKEN="$$(docker compose run --rm --no-deps -e TOKEN_EXPIRY_SECONDS --entrypoint python bootstrap /app/mint_token.py 2>/dev/null | tr -d '\r\n')"; \
	URL="$${MCP_PUBLIC_URL:-http://localhost:$${NGINX_PORT:-8080}/mcp}"; \
	printf '{\n  "mcpServers": {\n    "tableau-gateway": {\n      "command": "npx",\n      "args": [\n        "-y",\n        "mcp-remote",\n        "%s",\n        "--header",\n        "Authorization:Bearer %s"\n      ]\n    }\n  }\n}\n' "$$URL" "$$TOKEN"

identity-key:  ## Сгенерировать Fernet-ключ для TABLEAU_IDENTITY_ENC_KEY (в .env).
	@python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

clean:  ## Nuke volumes too.
	docker compose down -v

test-deps:  ## Поставить всё, что нужно pytest-набору (в активный venv/интерпретатор).
	python3 -m pip install -r tests/requirements.txt

test:  ## Прогнать pytest по всему стеку (contexts + bootstrap).
	cd tests && python3 -m pytest -v
