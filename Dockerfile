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

# Run the server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]