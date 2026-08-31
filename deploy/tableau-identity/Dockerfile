FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --upgrade pip \
 && pip install . \
 && pip install "psycopg[binary]>=3.1"

RUN adduser --disabled-password --gecos "" mcp \
 && mkdir -p /app/catalog \
 && chown -R mcp:mcp /app
USER mcp

EXPOSE 8020

CMD ["python", "-m", "tableau_identity.broker"]
