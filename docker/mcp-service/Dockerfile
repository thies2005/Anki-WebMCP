# Anki MCP Service Dockerfile
# Single headless multi-user service: MCP endpoint, token portal, and admin UI
# on one port. Collections are served in-process via the official `anki` wheel.
FROM python:3.14-slim

LABEL maintainer="Antigravity"
LABEL org.opencontainers.image.title="Anki MCP Service"
LABEL org.opencontainers.image.description="Headless multi-user Anki MCP server (per-user collections, token portal, AnkiWeb sync)"

WORKDIR /app

# Install curl for healthchecks; tzdata so the documented TZ env var works for
# non-UTC zones (created-at timestamps, log times).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl tzdata && \
    rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create the application user and the data directory (0700: it holds users.json
# with password hashes, MCP tokens, and AnkiWeb credentials at rest).
RUN useradd -m -u 1000 anki && \
    mkdir -p /data && \
    chown -R anki:anki /app /data && \
    chmod 700 /data

# Copy source code
COPY src /app/src

USER anki

EXPOSE 8000

VOLUME /data

# The service boots in seconds (no GUI to start); /health returns 503 when the
# users store is unreadable or the data dir is not writable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["python", "-m", "src.main"]
