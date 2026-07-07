# Official Tableau MCP server, HTTP transport.
#
# Two-stage: clone + build with dev deps, then a lean runtime image with only
# production deps. Pinned by TABLEAU_MCP_REF (tag or SHA) — passed from compose.

# ---------- Builder ----------
FROM node:22-alpine AS builder

ARG TABLEAU_MCP_REF=v2.22.0
ENV TABLEAU_MCP_REF=${TABLEAU_MCP_REF}

RUN apk add --no-cache git

WORKDIR /src
RUN git clone --depth 1 --branch "${TABLEAU_MCP_REF}" \
        https://github.com/tableau/tableau-mcp.git . \
 && npm ci --ignore-scripts \
 && npm run build

# ---------- Runtime ----------
FROM node:22-alpine AS runtime

ENV NODE_ENV=production \
    TRANSPORT=http \
    HTTP_PORT=3927

WORKDIR /app

# Copy only what's needed to run.
COPY --from=builder /src/package.json /src/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force

COPY --from=builder /src/build ./build

RUN addgroup -S mcp && adduser -S -G mcp mcp \
 && chown -R mcp:mcp /app
USER mcp

EXPOSE 3927

# Streamable HTTP endpoint: http://<host>:3927/tableau-mcp
CMD ["node", "build/index.js"]
