FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY __init__.py cli.py ./
COPY core/ ./core/
COPY models/ ./models/
COPY api/ ./api/
COPY scripts/ ./scripts/

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libpq-dev \
    gcc \
    && uv pip install --system --no-cache -e "." \
    && apt-get purge -y --auto-remove gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -r appuser && chown -R appuser:appuser /app && \
    chmod +x /app/scripts/ci/*.sh

USER appuser

ENTRYPOINT ["/app/scripts/ci/run.sh"]
