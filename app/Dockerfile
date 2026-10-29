# Builder stage: compile dependencies
FROM python:3.11-slim AS builder

WORKDIR /build

# Copy dependency specs only (for layer caching)
COPY pyproject.toml .

# Install build dependencies and create venv
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment and install dependencies.
# pip/setuptools/wheel are only needed to install, so remove them from the venv afterwards:
# they (and the libraries pip bundles) would otherwise ship in the runtime image with fixable CVEs.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==84.0.0 wheel==0.48.0 && \
    pip install --no-cache-dir -e . && \
    pip uninstall -y pip setuptools wheel

# Runtime stage: minimal production image
FROM python:3.11-slim

WORKDIR /app

# Install only curl for health checks (no build tools).
# The app runs from /opt/venv, so remove the base image's own setuptools/wheel:
# unused at runtime, and they carry fixable CVEs.
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/* && \
    pip uninstall -y --no-cache-dir setuptools wheel

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv

# Copy application code (src/app -> app)
COPY src/app ./app

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Set environment variables
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

# Expose port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
