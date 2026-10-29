# Moonwalk Cloud Orchestrator — Dockerfile for Google Cloud Run
# This image contains ONLY the "Brain" (LLM logic + cloud tools).
# macOS tools (osascript, pyobjc, etc.) are NOT included.

FROM python:3.11-slim AS builder

WORKDIR /build

# Install Python dependencies (cloud-only subset)
COPY backend/requirements-cloud.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements-cloud.txt

# ── Production image ──
FROM python:3.11-slim

# Create non-root user
RUN groupadd -r moonwalk && useradd -r -g moonwalk -d /app -s /sbin/nologin moonwalk

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy the modular backend packages
COPY backend/__init__.py .
COPY backend/runtime_state.py .
COPY backend/auth.py .
COPY backend/agent/ agent/
COPY backend/providers/ providers/
COPY backend/tools/ tools/
COPY backend/multi_agent/ multi_agent/
COPY backend/browser/ browser/
COPY backend/servers/cloud_server.py .

# Set ownership
RUN chown -R moonwalk:moonwalk /app

# Switch to non-root user
USER moonwalk

# Env vars are injected at runtime by Cloud Run (not baked into the image)

# Cloud Run uses PORT env var (default 8080)
ENV PORT=8080
ENV MOONWALK_CLOUD=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# Health check for container orchestrators
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["python", "cloud_server.py"]
