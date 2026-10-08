# Anki MCP Service Dockerfile
FROM python:3.12-slim

LABEL maintainer="Antigravity"
LABEL org.opencontainers.image.title="Anki MCP Service"
LABEL org.opencontainers.image.description="Remote Streamable HTTP MCP server for Anki Desktop"

WORKDIR /app

# Install curl for healthchecks
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create application user and data directory
RUN useradd -m -u 1000 appuser && \
    mkdir -p /data && \
    chown -R appuser:appuser /app /data

# Copy source code
COPY src /app/src

USER appuser

EXPOSE 8000

VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["python", "-m", "src.main"]
