# ===========================================
# Stage 1: Builder - Install dependencies and build the project
# ===========================================
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

# Set working directory
WORKDIR /app

# Set environment variables for uv optimization
ENV UV_CACHE_DIR=/opt/uv-cache
ENV UV_PYTHON_CACHE_DIR=/opt/uv-cache/python
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy dependency files first for better layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN --mount=type=cache,target=/opt/uv-cache \
    uv sync --locked --no-install-project --no-editable --no-dev

# Copy source code
COPY . .

# Install the project in non-editable mode for production
RUN --mount=type=cache,target=/opt/uv-cache \
    uv sync --locked --no-editable --no-dev

# ===========================================
# Stage 2: Runtime - Minimal Python slim image
# ===========================================
FROM python:3.13-slim-bookworm

# Set working directory
WORKDIR /app

# Copy the virtual environment from builder stage
COPY --from=builder /app/.venv /app/.venv

# Set environment variables for Python virtual environment
ENV PATH="/app/.venv/bin:$PATH"
# Override platformdirs to use /app/data for configuration
ENV XDG_CONFIG_HOME=/app/data
ENV XDG_DATA_HOME=/app/data

# Copy only the essential application files
COPY --from=builder /app/src /app/src

# Expose port for web interface
EXPOSE 8256

# Use the installed application directly via Python module
ENTRYPOINT ["nemorosa"]
