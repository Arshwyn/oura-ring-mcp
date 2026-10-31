FROM python:3.11.18-slim-trixie

# Install uv for fast package management
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

WORKDIR /app

# Copy files
COPY pyproject.toml .
COPY main.py .

# Install dependencies, then drop the base image's build tooling (not needed at runtime)
RUN uv pip install --system -r pyproject.toml \
    && uv pip uninstall --system setuptools wheel

# Run as a fixed, non-root user (no login shell, no home directory)
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid 1001 --no-create-home --shell /usr/sbin/nologin app

# HOME points at the tmpfs so libraries can cache there on a read-only root filesystem
ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1

USER 1001:1001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

# Run the server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]