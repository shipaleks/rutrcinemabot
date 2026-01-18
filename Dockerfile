# =============================================================================
# Media Concierge Bot - Docker Image
# =============================================================================
# Multi-stage build for optimized production image
# Final image size target: < 500MB
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder
# -----------------------------------------------------------------------------
FROM python:3.11-slim as builder

# Install system dependencies for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy project files needed for installation
COPY pyproject.toml README.md /tmp/
COPY src/ /tmp/src/
WORKDIR /tmp

# Install dependencies (without dev dependencies)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir .

# -----------------------------------------------------------------------------
# Stage 2: Runtime
# -----------------------------------------------------------------------------
FROM python:3.11-slim

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create app directory and non-root user
RUN useradd -m -u 1000 botuser && \
    mkdir -p /app/data && \
    chown -R botuser:botuser /app

# Set working directory
WORKDIR /app

# Copy application code
COPY --chown=botuser:botuser src/ /app/src/
COPY --chown=botuser:botuser data/.gitkeep /app/data/

# Switch to non-root user
USER botuser

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV LOG_LEVEL=INFO
ENV PORT=8000
ENV HEALTH_PORT=8080

# Health check endpoint on dedicated health port
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${HEALTH_PORT:-8080}/health || exit 1

# Expose both webhook and health check ports
EXPOSE 8000 8080

# Run the bot
CMD ["python", "-m", "src.bot.main"]
