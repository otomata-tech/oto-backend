# oto-mcp — image déployable (SaaS pull-registry OU on-premise client).
# Tout par variables d'env (cf. §Config ci-dessous) : un client pointe sa
# propre DB, son auth OIDC, sa master key. Aucun secret dans l'image.
# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /venv
ENV PATH=/venv/bin:$PATH PIP_NO_CACHE_DIR=1
RUN pip install -U pip wheel
# Lib connecteurs (oto-core, open source) — depuis git tant que pas sur PyPI.
# `[browser]` tire o-browser (PyPI) pour le client RemoteBrowser (LinkedIn délégué
# à o-browser-full, pas de chromium dans cette image).
RUN pip install "oto-core[browser] @ git+https://github.com/otomata-tech/oto-core.git@main"
COPY . /src/oto-mcp
RUN pip install /src/oto-mcp

FROM python:3.12-slim AS runtime
RUN useradd -r -u 10001 otomcp
COPY --from=builder /venv /venv
ENV PATH=/venv/bin:$PATH \
    MCP_TRANSPORT=streamable_http \
    HOST=0.0.0.0 \
    PORT=9103 \
    LOG_LEVEL=INFO
EXPOSE 9103
USER otomcp
# Config (env, jamais en dur) :
#   DATABASE_URL              PG (coffre + state). Le client fournit le sien.
#   LOGTO_ENDPOINT / MCP_AUDIENCE / OTO_MCP_PUBLIC_URL   auth OIDC.
#   OTO_MCP_OAUTH_STATE_SECRET   HMAC anti-CSRF.
#   OTO_MCP_ADMIN_SUB         sub admin bootstrap.
#   OTO_MCP_MASTER_KEY        chiffrement du coffre (hex64/base64-32o). Absente =
#                             dormant (secrets en clair). Le client la fournit
#                             depuis SON secret store.
#   OTO_CONFIG_DISABLE_SOPS=1 recommandé (pas de résolution SOPS serveur).
ENTRYPOINT ["oto-mcp"]
