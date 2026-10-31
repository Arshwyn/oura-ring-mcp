FROM python:3.11.18-slim-trixie

# Install uv for fast package management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# Copy files
COPY pyproject.toml .
COPY main.py .

# Install dependencies
RUN uv pip install --system -r pyproject.toml

# Run the server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]