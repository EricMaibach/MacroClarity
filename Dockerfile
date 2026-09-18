# SignalTrackers Financial Dashboard
# Multi-stage Docker build for production deployment

# =============================================================================
# Stage 1: Builder - Install dependencies
# =============================================================================
FROM python:3.13-slim AS builder

WORKDIR /build

# Install build dependencies (gcc needed for some Python packages)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY signaltrackers/requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Install gunicorn for production server
RUN pip install --no-cache-dir --user gunicorn

# =============================================================================
# Stage 2: Runtime - Minimal production image
# =============================================================================
FROM python:3.13-slim

# Labels for GitHub Container Registry
LABEL org.opencontainers.image.source="https://github.com/EricMaibach/MacroClarity"
LABEL org.opencontainers.image.description="SignalTrackers Financial Dashboard"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Copy application code
COPY signaltrackers/ .

# Create directories for persistent data and logs
RUN mkdir -p data logs

# Make entrypoint script executable
RUN chmod +x docker-entrypoint.sh

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Expose the application port
EXPOSE 5000

# Environment variables (can be overridden at runtime)
ENV PYTHONUNBUFFERED=1
ENV AI_PROVIDER=anthropic
# SECRET_KEY should be set at runtime for production

# Health check - verify the app is responding
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

# Run entrypoint script which handles migrations before starting gunicorn
# - Single worker (-w 1) ensures scheduler runs only once
# - --preload loads app before forking, required for APScheduler
# - Bind to all interfaces for container networking
CMD ["./docker-entrypoint.sh"]
